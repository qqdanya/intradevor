# strategies/oscar_grind_2.py
from __future__ import annotations

import asyncio
import math
from typing import Optional
from zoneinfo import ZoneInfo

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
    is_demo_account,
)
from core.signal_waiter import wait_for_signal_versioned, peek_signal_state
from strategies.base import StrategyBase
from core.policy import normalize_sprint
from core.money import format_amount

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Берём вспомогательные вещи из мартингейла, чтобы не дублировать
from strategies.martingale import (
    _minutes_from_timeframe,
    ALL_SYMBOLS_LABEL,
    ALL_TF_LABEL,
)
CLASSIC_ALLOWED_TFS = {"M5", "M15", "M30", "H1", "H4"}


DEFAULTS = {
    # Базовая «единица» ставки (unit)
    "base_investment": 100,
    # Цель серии по прибыли (в валюте счёта). По умолчанию == base_investment
    "target_profit": None,  # если None — будет подставлено base_investment при инициализации
    # Ограничения/повторения
    "max_steps": 20,  # максимум сделок в серии
    "repeat_count": 10,  # сколько серий подряд выполнять
    "min_balance": 100,  # ниже не торгуем
    # Фильтр по payout
    "min_percent": 70,
    "wait_on_low_percent": 1,  # задержка (сек) когда payout ниже минимума
    # Временные и прочие параметры
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    # Поведение серии: повторный вход после поражения
    "double_entry": True,
    "trade_type": "classic",
}


class OscarGrind2Strategy(StrategyBase):
    """Oscar Grind 2 (позитивная прогрессия).

    Вариант стратегии, в котором после выигрыша следующая ставка
    увеличивается на 1 «единицу», но не больше, чем необходимо для
    достижения целевой прибыли серии одним следующим выигрышем.
    """

    def __init__(
        self,
        http_client: HttpClient,
        user_id: str,
        user_hash: str,
        symbol: str,
        log_callback=None,
        *,
        timeframe: str = "M1",
        params: Optional[dict] = None,
        **_,
    ):
        p = dict(DEFAULTS)
        if params:
            p.update(params)

        # Если цель не задана — равна base_investment
        if p.get("target_profit") in (None, 0):
            p["target_profit"] = float(p.get("base_investment", 100))

        _symbol = (symbol or "").strip()
        _tf_raw = (timeframe or "").strip()
        _tf = _tf_raw.upper()

        self._use_any_symbol = _symbol == ALL_SYMBOLS_LABEL
        self._use_any_timeframe = _tf_raw == ALL_TF_LABEL

        cur_symbol = "*" if self._use_any_symbol else _symbol
        cur_tf = "*" if self._use_any_timeframe else _tf

        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=cur_symbol,
            log_callback=log_callback,
            **p,
        )

        self.http_client = http_client
        self.timeframe = cur_tf or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        raw_minutes = int(
            self.params.get("minutes", _minutes_from_timeframe(self.timeframe))
        )
        norm = normalize_sprint(cur_symbol, raw_minutes)
        if norm is None:
            fallback = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(cur_symbol, fallback) or fallback
            if self.log:
                self.log(
                    f"[{cur_symbol}] ⚠ Минуты {raw_minutes} недопустимы. Использую {norm}."
                )
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

        self._trade_type = str(self.params.get("trade_type", "sprint")).lower()
        self.params["trade_type"] = self._trade_type

        self._on_trade_result = self.params.get("on_trade_result")
        self._on_trade_pending = self.params.get("on_trade_pending")
        self._on_status = self.params.get("on_status")

        def _status(msg: str):
            cb = self._on_status
            if callable(cb):
                try:
                    cb(msg)
                except Exception:
                    pass

        self._status = _status

        self._running = False
        self._last_signal_ver: Optional[int] = None
        self._last_indicator: str = "-"
        self._last_signal_at_str: Optional[str] = None
        self._next_expire_dt = None

        anchor = str(
            self.params.get("account_currency", DEFAULTS["account_currency"])
        ).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor

        self._anchor_is_demo: Optional[bool] = None
        self._low_payout_notified = False

    async def run(self) -> None:
        self._running = True
        log = self.log or (lambda s: None)

        # Зафиксируем начальный режим счёта
        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Якорный режим счёта: {mode_txt}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        # Баланс при старте (информативно)
        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({format_amount(amount)}), текущая валюта: {cur_ccy}, якорь: {self._anchor_ccy}"
            )
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс при старте: {e}")

        series_left = int(self.params.get("repeat_count", DEFAULTS["repeat_count"]))
        if series_left <= 0:
            log(
                f"[{self.symbol}] 🛑 repeat_count={series_left} — нечего выполнять. Завершение."
            )
            self._running = False
            (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии.")
            return

        # Зафиксируем версию сигнала, если пара/ТФ не «*»
        try:
            if self.symbol != "*" and self.timeframe != "*":
                st = peek_signal_state(self.symbol, self.timeframe)
                self._last_signal_ver = st.get("version", 0) or 0
                log(
                    f"[{self.symbol}] Заякорена версия сигнала: v{self._last_signal_ver}"
                )
            else:
                self._last_signal_ver = 0
        except Exception:
            self._last_signal_ver = 0

        while self._running and series_left > 0:
            self._last_signal_at_str = None
            await self._pause_point()

            # Разрешаем * (все пары/ТФ)
            if self._use_any_symbol:
                self.symbol = "*"
            if self._use_any_timeframe:
                self.timeframe = "*"
                self.params["timeframe"] = self.timeframe

            if not await self._ensure_anchor_currency():
                continue
            if not await self._ensure_anchor_account_mode():
                continue

            # Проверка минимального баланса
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0
            min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
            if bal < min_balance:
                log(
                    f"[{self.symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Ожидание..."
                )
                await self.sleep(2.0)
                continue

            # Параметры серии
            base_unit = float(self.params.get("base_investment", 100))
            target_profit = float(
                self.params.get("target_profit", base_unit)
            )  # цель профита в валюте счёта
            max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(
                self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
            )
            sig_timeout = float(
                self.params.get("signal_timeout_sec", DEFAULTS["signal_timeout_sec"])
            )
            double_entry = bool(
                self.params.get("double_entry", DEFAULTS["double_entry"])
            )
            account_ccy = self._anchor_ccy

            if max_steps <= 0:
                log(
                    f"[{self.symbol}] ⚠ max_steps={max_steps} — серию не стартуем. Жду следующий сигнал."
                )
                await self.sleep(1.0)
                continue

            # Состояние серии Oscar Grind
            step_idx = 0
            cum_profit = 0.0  # накопленный профиль серии (может уходить в минус)
            stake = base_unit  # текущая ставка (unit-кратная)

            series_direction = None  # направление текущей ставки
            repeat_trade = False  # повторный вход после поражения

            # Основной цикл серии
            while self._running and step_idx < max_steps:
                await self._pause_point()

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                if series_direction is None and self.symbol == "*":
                    self._status("ожидание сигнала")
                    log(
                        f"[{self.symbol}] ⏳ Ожидание сигнала на {self.timeframe} (шаг {step_idx + 1})..."
                    )
                    try:
                        direction = await self.wait_signal(timeout=sig_timeout)
                        series_direction = 1 if int(direction) == 1 else 2
                    except asyncio.TimeoutError:
                        log(
                            f"[{self.symbol}] ⌛ Таймаут ожидания сигнала внутри серии — выхожу из серии."
                        )
                        break
                    continue

                # Узнаём payout на текущую ставку
                pct = await get_current_percent(
                    self.http_client,
                    investment=stake,
                    option=self.symbol,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                    trade_type=self._trade_type,
                )
                if pct is None:
                    self._status("ожидание процента")
                    log(f"[{self.symbol}] ⚠ Не получили % выплаты. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue
                if pct < min_pct:
                    self._status("ожидание высокого процента")
                    if not self._low_payout_notified:
                        log(
                            f"[{self.symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём..."
                        )
                        self._low_payout_notified = True
                    await self.sleep(wait_low)
                    continue
                if self._low_payout_notified:
                    log(
                        f"[{self.symbol}] ℹ Работа продолжается (текущий payout = {pct}%)"
                    )
                    self._low_payout_notified = False

                # Получаем направление, если ещё не задано
                if series_direction is None:
                    self._status("ожидание сигнала")
                    log(
                        f"[{self.symbol}] ⏳ Ожидание сигнала на {self.timeframe} (шаг {step_idx + 1})..."
                    )
                    try:
                        direction = await self.wait_signal(timeout=sig_timeout)
                        series_direction = 1 if int(direction) == 1 else 2
                    except asyncio.TimeoutError:
                        log(
                            f"[{self.symbol}] ⌛ Таймаут ожидания сигнала внутри серии — выхожу из серии."
                        )
                        break

                status = series_direction  # 1=UP, 2=DOWN

                # Контроль min_balance к текущей ставке
                try:
                    cur_balance, _, _ = await get_balance_info(
                        self.http_client, self.user_id, self.user_hash
                    )
                except Exception:
                    cur_balance = None
                min_floor = float(
                    self.params.get("min_balance", DEFAULTS["min_balance"])
                )
                if cur_balance is None or (cur_balance - stake) < min_floor:
                    log(
                        f"[{self.symbol}] 🛑 Сделка {format_amount(stake)} {account_ccy} может опустить баланс ниже "
                        f"{format_amount(min_floor)} {account_ccy}"
                        + (
                            ""
                            if cur_balance is None
                            else f" (текущий {format_amount(cur_balance)} {account_ccy})"
                        )
                        + ". Останавливаю стратегию."
                    )
                    self._running = False
                    break

                # Текущий режим счёта (для GUI)
                try:
                    demo_now = await is_demo_account(self.http_client)
                except Exception:
                    demo_now = False
                account_mode = "ДЕМО" if demo_now else "РЕАЛ"

                log(
                    f"[{self.symbol}] step={step_idx + 1} stake={format_amount(stake)} min={self._trade_minutes} "
                    f"side={'UP' if status == 1 else 'DOWN'} payout={pct}%"
                )

                # --- размещаем сделку ---
                self._status("делает ставку")
                trade_kwargs = {"trade_type": self._trade_type}
                time_arg = self._trade_minutes
                if self._trade_type == "classic":
                    if not self._next_expire_dt:
                        log(
                            f"[{self.symbol}] ❌ Нет времени экспирации для classic. Пауза и повтор."
                        )
                        await self.sleep(1.0)
                        continue
                    time_arg = self._next_expire_dt.strftime("%H:%M")
                    trade_kwargs["date"] = self._next_expire_dt.strftime("%d-%m-%Y")
                attempt = 0
                trade_id = None
                while attempt < 4:
                    trade_id = await place_trade(
                        self.http_client,
                        user_id=self.user_id,
                        user_hash=self.user_hash,
                        investment=stake,
                        option=self.symbol,
                        status=status,
                        minutes=time_arg,
                        account_ccy=account_ccy,
                        strict=True,
                        on_log=log,
                        **trade_kwargs,
                    )
                    if trade_id:
                        break
                    attempt += 1
                    if attempt < 4:
                        log(f"[{self.symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                        await self.sleep(1.0)
                if not trade_id:
                    log(
                        f"[{self.symbol}] ❌ Не удалось разместить сделку после 4 попыток. Ждём новый сигнал."
                    )
                    series_direction = None
                    continue

                # определяем длительность сделки (для таймера и ожидания результата)
                from datetime import datetime

                if (
                    self._trade_type == "classic"
                    and self._next_expire_dt is not None
                ):
                    trade_seconds = max(
                        0.0,
                        (
                            self._next_expire_dt - datetime.now(MOSCOW_TZ)
                        ).total_seconds(),
                    )
                    expected_end_ts = self._next_expire_dt.timestamp()
                else:
                    trade_seconds = float(self._trade_minutes) * 60.0
                    expected_end_ts = datetime.now().timestamp() + trade_seconds

                wait_seconds = self.params.get("result_wait_s")
                if wait_seconds is None:
                    wait_seconds = trade_seconds
                else:
                    wait_seconds = float(wait_seconds)

                # GUI: ожидаем результат (две метки времени)
                placed_at_str = datetime.now().strftime("%d.%м.%Y %H:%M:%S")
                if callable(self._on_trade_pending):
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=self._last_signal_at_str,
                            placed_at=placed_at_str,
                            direction=status,
                            stake=float(stake),
                            percent=int(pct),
                            wait_seconds=float(trade_seconds),
                            account_mode=account_mode,
                            indicator=self._last_indicator,
                            expected_end_ts=expected_end_ts,
                        )
                    except Exception:
                        pass

                self._status("ожидание результата")

                profit = await check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=wait_seconds,
                )

                # GUI: результат
                if callable(self._on_trade_result):
                    try:
                        self._on_trade_result(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=self._last_signal_at_str,
                            placed_at=placed_at_str,
                            direction=status,
                            stake=float(stake),
                            percent=int(pct),
                            profit=(None if profit is None else float(profit)),
                            account_mode=account_mode,
                            indicator=self._last_indicator,
                        )
                    except Exception:
                        pass

                # Обновим накопленный профит
                if profit is None:
                    log(f"[{self.symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    cum_profit -= float(stake)
                    outcome = "loss"
                else:
                    cum_profit += float(profit)
                    if profit > 0.0:
                        outcome = "win"
                    elif profit == 0.0:
                        outcome = "refund"
                    else:
                        outcome = "loss"

                # Проверим цель
                if cum_profit >= target_profit:
                    log(
                        f"[{self.symbol}] ✅ Серия завершена: достигнута цель {format_amount(target_profit)} "
                        f"(накоплено {format_amount(cum_profit)})."
                    )
                    step_idx += 1
                    break

                # Вычислим следующую ставку по правилам Oscar Grind
                need = max(0.0, target_profit - cum_profit)
                next_stake = self._next_stake(
                    outcome=outcome,
                    stake=stake,
                    base_unit=base_unit,
                    pct=pct,
                    need=need,
                    profit=0.0 if profit is None else float(profit),
                    cum_profit=cum_profit,
                    target_profit=target_profit,
                    log=log,
                )

                # Переходим к следующему шагу
                stake = float(next_stake)
                step_idx += 1
                if repeat_trade:
                    repeat_trade = False
                    series_direction = None
                else:
                    if double_entry and outcome == "loss":
                        repeat_trade = True
                    else:
                        series_direction = None
                await self.sleep(0.2)

            if not self._running:
                break

            if step_idx == 0:
                log(
                    f"[{self.symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серий осталось: {series_left}."
                )
            else:
                if step_idx >= max_steps:
                    log(
                        f"[{self.symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии."
                    )
                series_left -= 1
                log(f"[{self.symbol}] ▶ Осталось серий: {series_left}")

        self._running = False
        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии.")

    def _next_stake(
        self,
        *,
        outcome: str,
        stake: float,
        base_unit: float,
        pct: float,
        need: float,
        profit: float,
        cum_profit: float,
        target_profit: float,
        log,
    ) -> float:
        k = pct / 100.0
        if outcome == "win":
            next_req = math.ceil(need / k) if k > 0 else stake
            next_stake = max(base_unit, min(stake + base_unit, float(next_req)))
            log(
                f"[{self.symbol}] ✅ WIN: profit={format_amount(profit)}. "
                f"Накоплено {format_amount(cum_profit)}/{format_amount(target_profit)}. "
                f"Следующая ставка = min(stake+unit, req) → {format_amount(stake + base_unit)} / {format_amount(next_req)} = {format_amount(next_stake)}"
            )
        else:
            next_stake = stake
            if outcome == "refund":
                log(
                    f"[{self.symbol}] ↩️ REFUND: ставка возвращена. "
                    f"Следующая ставка остаётся {format_amount(next_stake)}."
                )
            else:
                log(
                    f"[{self.symbol}] ❌ LOSS: profit={format_amount(profit)}. "
                    f"Следующая ставка остаётся {format_amount(next_stake)}."
                )
        return float(next_stake)

    async def _ensure_anchor_currency(self) -> bool:
        try:
            _, ccy_now, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            ccy_now = None
        if ccy_now != self._anchor_ccy:
            self._status(f"ожидание смены валюты на {self._anchor_ccy}")
            await self.sleep(1.0)
            return False
        return True

    async def _ensure_anchor_account_mode(self) -> bool:
        try:
            demo_now = await is_demo_account(self.http_client)
        except Exception:
            self._status("ожидание проверки режима счёта")
            await self.sleep(1.0)
            return False

        if self._anchor_is_demo is None:
            self._anchor_is_demo = bool(demo_now)

        if bool(demo_now) != bool(self._anchor_is_demo):
            need = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            self._status(f"ожидание смены счёта на {need}")
            await self.sleep(1.0)
            return False
        return True

    async def wait_signal(self, *, timeout: float) -> int:
        grace = float(self.params.get("grace_delay_sec", DEFAULTS["grace_delay_sec"]))

        def _on_delay(sec: float):
            (self.log or (lambda s: None))(
                f"[{self.symbol}] ⏱ Задержка следующего прогноза ~{sec:.1f}s"
            )

        while True:
            coro = wait_for_signal_versioned(
                self.symbol,
                self.timeframe,
                since_version=self._last_signal_ver,
                check_pause=self.is_paused,
                timeout=None,
                raise_on_timeout=True,
                grace_delay_sec=grace,
                on_delay=_on_delay,
                include_meta=True,
                max_age_sec=(120.0 if self._trade_type == "classic" else 0.0),
            )

            direction, ver, meta = await self.wait_cancellable(coro, timeout=timeout)
            sig_symbol = (meta or {}).get("symbol") or self.symbol
            sig_tf = ((meta or {}).get("timeframe") or self.timeframe).upper()

            if (
                self._use_any_timeframe
                and self._trade_type == "classic"
                and sig_tf not in CLASSIC_ALLOWED_TFS
            ):
                self._last_signal_ver = ver
                if self.log:
                    self.log(
                        f"[{sig_symbol}] ⚠ Таймфрейм {sig_tf} недоступен для Classic — пропуск."
                    )
                continue

            self._last_signal_ver = ver
            self._last_indicator = (meta or {}).get("indicator") or "-"

            ts = (meta or {}).get("next_timestamp")
            self._next_expire_dt = ts.astimezone(MOSCOW_TZ) if ts else None

            if self._use_any_symbol:
                self.symbol = sig_symbol
            if self._use_any_timeframe:
                self.timeframe = sig_tf
                self.params["timeframe"] = self.timeframe
                raw = _minutes_from_timeframe(self.timeframe)
                norm = normalize_sprint(self.symbol, raw) or raw
                self._trade_minutes = int(norm)
                self.params["minutes"] = self._trade_minutes

            from datetime import datetime

            self._last_signal_at_str = datetime.now().strftime(
                "%d.%m.%Y %H:%M:%S"
            )
            return int(direction)

    def update_params(self, **params):
        super().update_params(**params)

        if "minutes" in params:
            try:
                requested = int(params["minutes"])
            except Exception:
                return
            norm = normalize_sprint(self.symbol, requested)
            if norm is None:
                if self.symbol == "BTCUSDT":
                    norm = 5 if requested < 5 else 500
                else:
                    norm = 1 if requested < 3 else max(3, min(500, requested))
                if self.log:
                    self.log(
                        f"[{self.symbol}] ⚠ Минуты {requested} недопустимы. Исправлено на {norm}."
                    )
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

        if "timeframe" in params:
            tf_raw = str(params["timeframe"]).strip()
            tf = tf_raw.upper()
            self._use_any_timeframe = tf_raw in (ALL_TF_LABEL, "*")
            self.timeframe = "*" if self._use_any_timeframe else tf
            self.params["timeframe"] = self.timeframe
            if "minutes" not in params:
                raw = _minutes_from_timeframe(self.timeframe)
                norm = normalize_sprint(self.symbol, raw) or raw
                self._trade_minutes = int(norm)
                self.params["minutes"] = self._trade_minutes

        if "account_currency" in params:
            want = str(params["account_currency"]).upper()
            if want != self._anchor_ccy and self.log:
                self.log(
                    f"[{self.symbol}] ⚠ Игнорирую попытку сменить валюту на лету "
                    f"{self._anchor_ccy} → {want}. Валюта зафиксирована при создании."
                )
            self.params["account_currency"] = self._anchor_ccy

        if "trade_type" in params:
            self._trade_type = str(params["trade_type"]).lower()
            self.params["trade_type"] = self._trade_type

        if "base_investment" in params and "target_profit" not in params:
            # если юзер поменял unit, а цель оставил None — синхронизируем цель с unit
            try:
                unit = float(params["base_investment"])
            except Exception:
                unit = float(self.params.get("base_investment", 100))
            if self.params.get("target_profit") in (None, 0):
                self.params["target_profit"] = unit
