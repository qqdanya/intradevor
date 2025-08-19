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
)
from core.signal_waiter import wait_for_signal_versioned
from strategies.base import StrategyBase
from core.policy import normalize_sprint


DEFAULTS = {
    "base_investment": 100,  # базовая ставка
    "max_steps": 5,  # максимум колен в серии
    "repeat_count": 10,  # сколько серий подряд пытаться
    "min_balance": 100,  # ниже не торгуем
    "coefficient": 2.0,  # множитель мартингейла
    "min_percent": 70,  # минимальный % выплаты
    "wait_on_low_percent": 1,  # пауза если % низкий (сек)
    "signal_timeout_sec": 3600,  # таймаут ожидания сигнала (сек)
    "account_currency": "RUB",  # валюта счёта
    "result_wait_s": 60.0,  # сколько ждать результата сделки (сек)
    "grace_delay_sec": 30.0,  # сколько секунд терпим задержку следующего прогноза
}


def _minutes_from_timeframe(tf: str) -> int:
    # "M1","M5","M15","M30","H1","H4","D1","W1" -> минуты для sprint
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
    Совместима с Bot/MainWindow.
    Ожидание сигнала — версионное: каждый бот хранит self._last_signal_ver,
    'none' очищает состояние и не возвращается из ожидателя.
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
        # Объединим дефолты с пришедшими параметрами
        p = dict(DEFAULTS)
        if params:
            p.update(params)

        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            **p,
        )

        # для Bot.cleanup()
        self.http_client = http_client

        # таймфрейм сигнала (может отличаться от минут спринта)
        self.timeframe = timeframe or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        # минуты для сделки — нормализуем под политику
        raw_minutes = int(
            self.params.get("minutes", _minutes_from_timeframe(self.timeframe))
        )
        norm = normalize_sprint(self.symbol, raw_minutes)
        if norm is None:
            # fallback: из таймфрейма
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
        self._running = False

        # последняя увиденная версия сигнала (для исключения повторов)
        self._last_signal_ver: Optional[int] = None

    # ---- основной сценарий ----
    async def run(self):
        self._running = True
        log = self.log or (lambda s: None)

        # sanity: баланс и валюта
        try:
            amount, ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({amount:.2f}), валюта счёта: {ccy}"
            )
            self.params.setdefault(
                "account_currency", ccy or self.params.get("account_currency")
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

        while self._running and series_left > 0:
            await self._pause_point()

            # грубая проверка баланса на входе в серию
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0
            if bal < float(self.params.get("min_balance", DEFAULTS["min_balance"])):
                log(f"[{self.symbol}] ⛔ Баланс ниже минимума. Ожидание...")
                await self.sleep(2.0)
                continue

            # серия мартингейла до WIN или исчерпания шагов
            step = 0
            did_place_any_trade = False

            # начальная ставка для серии
            stake = float(
                self.params.get("base_investment", DEFAULTS["base_investment"])
            )
            coeff = float(self.params.get("coefficient", DEFAULTS["coefficient"]))
            max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(
                self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
            )
            account_ccy = str(
                self.params.get("account_currency", DEFAULTS["account_currency"])
            )
            result_wait_s = float(
                self.params.get("result_wait_s", DEFAULTS["result_wait_s"])
            )

            if max_steps <= 0:
                log(
                    f"[{self.symbol}] ⚠ max_steps={max_steps} — серия не может стартовать. Жду следующий сигнал."
                )
                await self.sleep(1.0)
                continue

            while self._running and step < max_steps:
                await self._pause_point()

                # ✅ ждём НОВЫЙ сигнал КАЖДЫЙ РАЗ перед коленом
                log(
                    f"[{self.symbol}] ⏳ Ожидание сигнала на {self.timeframe} (шаг {step})..."
                )
                try:
                    direction = await self.wait_signal(
                        timeout=float(
                            self.params.get(
                                "signal_timeout_sec", DEFAULTS["signal_timeout_sec"]
                            )
                        )
                    )
                except asyncio.TimeoutError:
                    log(
                        f"[{self.symbol}] ⌛ Таймаут ожидания сигнала внутри серии — выхожу из серии."
                    )
                    break  # из серии; внешняя петля попробует начать новую позже

                status = 1 if direction == 1 else 2  # 1=up/buy, 2=down/sell

                # текущий % выплаты
                pct = await get_current_percent(
                    self.http_client,
                    investment=stake,
                    option=self.symbol,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                )
                if pct is None:
                    log(f"[{self.symbol}] ⚠ Не получили % выплаты. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue
                if pct < min_pct:
                    log(
                        f"[{self.symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём {wait_low}s"
                    )
                    await self.sleep(wait_low)
                    continue

                # ⛔ страховка дна: если проиграем, остаток не должен упасть ниже min_balance
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
                        f"[{self.symbol}] 🛑 Сделка {stake:.2f} {account_ccy} может опустить баланс "
                        f"ниже минимума {min_floor:.2f} {account_ccy}"
                        + (
                            ""
                            if cur_balance is None
                            else f" (текущий {cur_balance:.2f} {account_ccy})"
                        )
                        + ". Останавливаю стратегию."
                    )
                    self._running = False
                    break  # выйти из серии немедленно

                log(
                    f"[{self.symbol}] step={step} stake={stake:.2f} min={self._trade_minutes} "
                    f"side={'UP' if status == 1 else 'DOWN'} payout={pct}%"
                )

                # сделка
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

                # сообщаем в UI, что сделка размещена и ждём результата
                if callable(self._on_trade_pending):
                    from datetime import datetime

                    placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            placed_at=placed_at_str,
                            direction=status,  # 1/2
                            stake=float(stake),
                            percent=int(pct),
                            wait_seconds=float(result_wait_s),
                        )
                    except Exception:
                        pass

                did_place_any_trade = True

                # ждём результат
                profit = await check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=result_wait_s,
                )

                # коллбек в GUI (если задан)
                if callable(self._on_trade_result):
                    from datetime import datetime

                    placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_result(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            placed_at=placed_at_str,
                            direction=status,  # 1/2
                            stake=float(stake),
                            percent=int(pct),
                            profit=(None if profit is None else float(profit)),
                        )
                    except Exception:
                        pass

                # ---- интерпретация результата: WIN / LOSS / PUSH / UNKNOWN ----
                if profit is None:
                    log(f"[{self.symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    step += 1
                    stake *= coeff
                elif profit > 0:
                    log(
                        f"[{self.symbol}] ✅ WIN: profit={profit:.2f}. Серия завершена, откат к базе."
                    )
                    break
                elif abs(profit) < 1e-9:  # == 0: PUSH
                    log(
                        f"[{self.symbol}] 🤝 PUSH: возврат ставки. Повтор шага без увеличения."
                    )
                    # step/stake не меняем — повторим попытку на том же колене
                else:  # profit < 0
                    log(
                        f"[{self.symbol}] ❌ LOSS: profit={profit:.2f}. Увеличиваем ставку."
                    )
                    step += 1
                    stake *= coeff

                await self.sleep(0.2)

            # если стратегию попросили остановить внутри серии — выходим немедленно
            if not self._running:
                break

            if not did_place_any_trade:
                log(
                    f"[{self.symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серии осталось: {series_left}."
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

    async def wait_signal(self, *, timeout: float) -> int:
        """
        Ожидает НОВЫЙ для этого бота сигнал (версия > self._last_signal_ver).
        'none' не возвращается (только очищает состояние).
        Таймаут обрабатывается снаружи через wait_cancellable.
        """
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
            timeout=None,  # общий таймаут дадим через wait_cancellable
            raise_on_timeout=True,
            grace_delay_sec=grace,
            on_delay=_on_delay,
        )

        direction, ver = await self.wait_cancellable(coro, timeout=timeout)
        self._last_signal_ver = ver
        return int(direction)

    # --- горячее обновление параметров ---
    def update_params(self, **params):
        super().update_params(**params)

        if "minutes" in params:
            try:
                requested = int(params["minutes"])
            except Exception:
                return
            norm = normalize_sprint(self.symbol, requested)
            if norm is None:
                # мягкая коррекция к допустимому
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
            self.timeframe = str(params["timeframe"]) or self.timeframe
            if "minutes" not in params:
                self._trade_minutes = _minutes_from_timeframe(self.timeframe)
