# strategies/fibonacci.py
from __future__ import annotations

import asyncio
from typing import Optional

from strategies.martingale import MartingaleStrategy, DEFAULTS as MG_DEFAULTS
from zoneinfo import ZoneInfo
from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
    is_demo_account,
)
from core.money import format_amount

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Переиспользуем функции ожидания сигнала и прочие методы из MartingaleStrategy
# через наследование. Здесь определяем собственные значения по умолчанию
# без коэффициента умножения.
DEFAULTS = dict(MG_DEFAULTS)
DEFAULTS.pop("coefficient", None)


def _fib(n: int) -> int:
    """Возвращает n-е число Фибоначчи (1-indexed)."""
    seq = [1, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq[n - 1]


class FibonacciStrategy(MartingaleStrategy):
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
        # объединяем параметры с локальными значениями по умолчанию
        p = dict(DEFAULTS)
        if params:
            p.update(params)
        super().__init__(
            http_client,
            user_id,
            user_hash,
            symbol,
            log_callback,
            timeframe=timeframe,
            params=p,
        )
        # параметр coefficient не используется в этой стратегии
        self.params.pop("coefficient", None)

    async def run(self) -> None:  # noqa: C901 - сложность как у базовых стратегий
        self._running = True
        log = self.log or (lambda s: None)

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Режим счёта: {mode_txt}")
        except Exception as e:  # pragma: no cover - сеть
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({format_amount(amount)}), текущая валюта: {cur_ccy}"
            )
        except Exception as e:  # pragma: no cover - сеть
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
            if self.symbol != "*" and self.timeframe != "*":
                from core.signal_waiter import peek_signal_state

                st = peek_signal_state(self.symbol, self.timeframe)
                self._last_signal_ver = st.get("version", 0) or 0
                if self.log:
                    self.log(
                        f"[{self.symbol}] Заякорена версия сигнала: v{self._last_signal_ver}"
                    )
            else:
                self._last_signal_ver = 0
        except Exception:  # pragma: no cover
            self._last_signal_ver = 0

        next_start_step = 1
        while self._running and series_left > 0:
            self._last_signal_at_str = None
            await self._pause_point()

            if self._use_any_symbol:
                self.symbol = "*"
            if self._use_any_timeframe:
                self.timeframe = "*"
                self.params["timeframe"] = self.timeframe

            if not await self._ensure_anchor_currency():
                continue
            if not await self._ensure_anchor_account_mode():
                continue

            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:  # pragma: no cover
                bal = 0.0

            min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
            if bal < min_balance:
                log(
                    f"[{self.symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Ожидание..."
                )
                await self.sleep(2.0)
                continue

            base = float(
                self.params.get("base_investment", DEFAULTS["base_investment"])
            )
            max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(
                self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
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

            step = next_start_step
            did_place_any_trade = False
            series_direction = None

            while self._running and step <= max_steps:
                await self._pause_point()

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                stake = base * _fib(step)

                if series_direction is None and self.symbol == "*":
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
                    continue

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

                try:
                    cur_balance, _, _ = await get_balance_info(
                        self.http_client, self.user_id, self.user_hash
                    )
                except Exception:  # pragma: no cover
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

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                log(
                    f"[{self.symbol}] step={step} stake={format_amount(stake)} min={self._trade_minutes} "
                    f"side={'UP' if status == 1 else 'DOWN'} payout={pct}%"
                )

                try:
                    demo_now = await is_demo_account(self.http_client)
                except Exception:
                    demo_now = False
                account_mode = "ДЕМО" if demo_now else "РЕАЛ"

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

                did_place_any_trade = True

                # определяем длительность сделки (для таймера и ожидания результата)
                from datetime import datetime

                if self._trade_type == "classic" and self._next_expire_dt is not None:
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

                placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                trade_symbol = self.symbol
                trade_timeframe = self.timeframe
                if callable(self._on_trade_pending):
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=trade_symbol,
                            timeframe=trade_timeframe,
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

                self._register_pending_trade(trade_id, trade_symbol, trade_timeframe)

                ctx = dict(
                    trade_id=trade_id,
                    wait_seconds=float(wait_seconds),
                    placed_at=placed_at_str,
                    signal_at=self._last_signal_at_str,
                    symbol=trade_symbol,
                    timeframe=trade_timeframe,
                    direction=status,
                    stake=float(stake),
                    percent=int(pct),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                )

                if self._allow_parallel_trades:
                    task = asyncio.create_task(
                        self._wait_for_trade_result(**ctx)
                    )
                    self._launch_trade_result_task(task)
                    profit = await task
                else:
                    profit = await self._wait_for_trade_result(**ctx)

                if profit is None:
                    log(f"[{self.symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    step += 1
                elif profit > 0:
                    log(
                        f"[{self.symbol}] ✅ WIN: profit={format_amount(profit)}. Откат на два шага назад."
                    )
                    next_start_step = max(1, step - 2)
                    break
                elif abs(profit) < 1e-9:
                    log(
                        f"[{self.symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения."
                    )
                else:
                    log(
                        f"[{self.symbol}] ❌ LOSS: profit={format_amount(profit)}. Переход к следующему числу."
                    )
                    step += 1

                await self.sleep(0.2)

            if not self._running:
                break

            if not did_place_any_trade:
                log(
                    f"[{self.symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серий осталось: {series_left}."
                )
            else:
                if step > max_steps:
                    log(
                        f"[{self.symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии."
                    )
                    next_start_step = 1
                series_left -= 1
                log(f"[{self.symbol}] ▶ Осталось серий: {series_left}")

        self._running = False

        await self._cancel_signal_listener()

        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)
            self._pending_tasks.clear()

        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии.")
