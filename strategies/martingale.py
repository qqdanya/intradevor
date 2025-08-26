# strategies/martingale.py
from __future__ import annotations

import asyncio
from typing import Optional

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

ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"

DEFAULTS = {
    "base_investment": 100,
    "max_steps": 5,
    "repeat_count": 10,
    "min_balance": 100,
    "coefficient": 2.0,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
}


def _minutes_from_timeframe(tf: str) -> int:
    if not tf:
        return 1
    unit = tf[0].upper()
    try:
        n = int(tf[1:])
    except Exception:
        return 1
    if unit == "M":
        return n
    if unit == "H":
        return n * 60
    if unit == "D":
        return n * 60 * 24
    if unit == "W":
        return n * 60 * 24 * 7
    return 1


class MartingaleStrategy(StrategyBase):
    """
    Мартингейл — работает ТОЛЬКО по одной паре и ОДНОМУ таймфрейму.
    Если в UI выбраны «Все валютные пары»/«Все таймфреймы», мы корректно
    приводим к одиночным (символ из kwargs и TF=M1) и пишем предупреждение в лог.
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

        # Нормализация «все» → одиночные
        _symbol = (symbol or "").strip()
        _tf = (timeframe or "").strip().upper()
        if _symbol == ALL_SYMBOLS_LABEL:
            _symbol = params.get("force_symbol", "") or "EUR/USD"
        if _tf == ALL_TF_LABEL:
            _tf = "M1"

        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=_symbol,
            log_callback=log_callback,
            **p,
        )

        self.http_client = http_client
        self.timeframe = _tf or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        raw_minutes = int(
            self.params.get("minutes", _minutes_from_timeframe(self.timeframe))
        )
        norm = normalize_sprint(self.symbol, raw_minutes)
        if norm is None:
            fallback = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, fallback) or fallback
            if self.log:
                self.log(
                    f"[{self.symbol}] ⚠ Минуты {raw_minutes} недопустимы. Использую {norm}."
                )
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

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
        self._last_signal_at_str: Optional[str] = (
            None  # <=== НОВОЕ: время прихода сигнала
        )
        self._last_signal_mono: Optional[float] = None  # loop.time() прихода сигнала

        anchor = str(
            self.params.get("account_currency", DEFAULTS["account_currency"])
        ).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor

        self._anchor_is_demo: Optional[bool] = None

    async def run(self) -> None:
        self._running = True
        log = self.log or (lambda s: None)

        # Подсказка, если кто-то пытался запустить «на всех»
        if self.symbol == ALL_SYMBOLS_LABEL or self.timeframe == ALL_TF_LABEL:
            log(
                "⚠ Мартингейл поддерживает только одну валютную пару и один таймфрейм. "
                "Привожу к одиночным параметрам (TF=M1)."
            )

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Якорный режим счёта: {mode_txt}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({amount:.2f}), текущая валюта: {cur_ccy}, якорь: {self._anchor_ccy}"
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

        try:
            st = peek_signal_state(self.symbol, self.timeframe)
            self._last_signal_ver = st.get("version", 0) or 0
            if self.log:
                self.log(
                    f"[{self.symbol}] Заякорена версия сигнала: v{self._last_signal_ver}"
                )
        except Exception:
            self._last_signal_ver = 0

        while self._running and series_left > 0:
            await self._pause_point()

            if not await self._ensure_anchor_currency():
                continue
            if not await self._ensure_anchor_account_mode():
                continue

            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0

            min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
            if bal < min_balance:
                log(
                    f"[{self.symbol}] ⛔ Баланс ниже минимума ({bal:.2f} < {min_balance:.2f}). Ожидание..."
                )
                await self.sleep(2.0)
                continue

            stake = float(
                self.params.get("base_investment", DEFAULTS["base_investment"])
            )
            coeff = float(self.params.get("coefficient", DEFAULTS["coefficient"]))
            max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(
                self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
            )
            result_wait_s = float(
                self.params.get("result_wait_s", DEFAULTS["result_wait_s"])
            )
            sig_timeout = float(
                self.params.get("signal_timeout_sec", DEFAULTS["signal_timeout_sec"])
            )
            account_ccy = self._anchor_ccy

            if max_steps <= 0:
                log(
                    f"[{self.symbol}] ⚠ max_steps={max_steps} — серию не стартуем. Жду следующий сигнал."
                )
                await self.sleep(1.0)
                continue

            step = 0
            did_place_any_trade = False
            series_direction = None

            while self._running and step < max_steps:
                await self._pause_point()

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                if series_direction is None:
                    self._status("ожидание сигнала")
                    log(
                        f"[{self.symbol}] ⏳ Ожидание сигнала на {self.timeframe} (шаг {step})..."
                    )
                    try:
                        direction = await self.wait_signal(timeout=sig_timeout)
                    except asyncio.TimeoutError:
                        log(
                            f"[{self.symbol}] ⌛ Таймаут ожидания сигнала внутри серии — выхожу из серии."
                        )
                        break
                    series_direction = 1 if int(direction) == 1 else 2

                status = series_direction

                # payout
                pct = await get_current_percent(
                    self.http_client,
                    investment=stake,
                    option=self.symbol,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                )
                if pct is None:
                    self._status("ожидание процента")
                    log(f"[{self.symbol}] ⚠ Не получили % выплаты. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue
                if pct < min_pct:
                    self._status("ожидание высокого процента")
                    log(
                        f"[{self.symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём {wait_low}s"
                    )
                    await self.sleep(wait_low)
                    continue

                # защита min_balance
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
                        f"[{self.symbol}] 🛑 Сделка {stake:.2f} {account_ccy} может опустить баланс ниже "
                        f"{min_floor:.2f} {account_ccy}"
                        + (
                            ""
                            if cur_balance is None
                            else f" (текущий {cur_balance:.2f} {account_ccy})"
                        )
                        + ". Останавливаю стратегию."
                    )
                    self._running = False
                    break

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                log(
                    f"[{self.symbol}] step={step} stake={stake:.2f} min={self._trade_minutes} "
                    f"side={'UP' if status == 1 else 'DOWN'} payout={pct}%"
                )

                try:
                    demo_now = await is_demo_account(self.http_client)
                except Exception:
                    demo_now = False
                account_mode = "ДЕМО" if demo_now else "РЕАЛ"

                # --- размещаем сделку ---
                self._status("делает ставку")
                trade_id = await place_trade(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    investment=stake,
                    option=self.symbol,
                    status=status,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                    strict=True,
                    on_log=log,
                )
                if not trade_id:
                    log(f"[{self.symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue

                did_place_any_trade = True

                # GUI: ожидание результата (с двумя временами)
                if callable(self._on_trade_pending):
                    from datetime import datetime

                    placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=(self._last_signal_at_str or placed_at_str),
                            placed_at=placed_at_str,
                            direction=status,
                            stake=float(stake),
                            percent=int(pct),
                            wait_seconds=float(result_wait_s),
                            account_mode=account_mode,
                            indicator=self._last_indicator,
                        )
                    except Exception:
                        pass

                self._status("ожидание результата")

                # подсчитываем, сколько времени прошло с момента прихода сигнала
                delay = 0.0
                if self._last_signal_mono is not None:
                    delay = asyncio.get_running_loop().time() - self._last_signal_mono

                remaining = max(float(result_wait_s) - delay, 0.0)
                pre_wait = max(remaining - 5.0, 0.0)

                profit = await check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=remaining,
                    initial_wait=pre_wait,
                )

                if callable(self._on_trade_result):
                    from datetime import datetime

                    placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_result(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=(self._last_signal_at_str or placed_at_str),
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

                if profit is None:
                    log(f"[{self.symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    step += 1
                    stake *= coeff
                elif profit > 0:
                    log(
                        f"[{self.symbol}] ✅ WIN: profit={profit:.2f}. Серия завершена, откат к базе."
                    )
                    break
                elif abs(profit) < 1e-9:
                    log(
                        f"[{self.symbol}] 🤝 PUSH: возврат ставки. Повтор шага без увеличения."
                    )
                else:
                    log(
                        f"[{self.symbol}] ❌ LOSS: profit={profit:.2f}. Увеличиваем ставку."
                    )
                    step += 1
                    stake *= coeff

                await self.sleep(0.2)

            if not self._running:
                break

            if not did_place_any_trade:
                log(
                    f"[{self.symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серий осталось: {series_left}."
                )
            else:
                if step >= max_steps:
                    log(
                        f"[{self.symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии."
                    )
                series_left -= 1
                log(f"[{self.symbol}] ▶ Осталось серий: {series_left}")

        self._running = False
        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии.")

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
        )

        direction, ver, meta = await self.wait_cancellable(coro, timeout=timeout)
        self._last_signal_ver = ver
        self._last_indicator = (meta or {}).get("indicator") or "-"
        # фиксируем момент прихода сигнала — для таблицы "Время получения сигнала"
        from datetime import datetime

        self._last_signal_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self._last_signal_mono = asyncio.get_running_loop().time()
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
            tf = str(params["timeframe"]).strip().upper()
            if tf == ALL_TF_LABEL:
                tf = "M1"
                if self.log:
                    self.log(
                        f"[{self.symbol}] ⚠ Мартингейл не поддерживает «Все таймфреймы». Установлено M1."
                    )
            self.timeframe = tf
            if "minutes" not in params:
                self._trade_minutes = _minutes_from_timeframe(self.timeframe)

        if "account_currency" in params:
            want = str(params["account_currency"]).upper()
            if want != self._anchor_ccy and self.log:
                self.log(
                    f"[{self.symbol}] ⚠ Игнорирую попытку сменить валюту на лету "
                    f"{self._anchor_ccy} → {want}. Валюта зафиксирована при создании."
                )
            self.params["account_currency"] = self._anchor_ccy
