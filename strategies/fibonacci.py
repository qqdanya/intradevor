# strategies/fibonacci.py
from __future__ import annotations

import asyncio
from datetime import datetime
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

    async def _process_single_signal(self, signal_data: dict):
        """Обрабатывает один сигнал независимо от других - переопределяем для Фибоначчи"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала Фибоначчи")
        
        # Обновляем последнюю информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")
        
        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(MOSCOW_TZ) if ts else None

        # Обновляем символ и таймфрейм если используются "все"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe
            raw = self._minutes_from_timeframe(self.timeframe)
            norm = self._normalize_sprint(self.symbol, raw) or raw
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # Запускаем серию Фибоначчи для этого сигнала
        await self._run_fibonacci_series(symbol, timeframe, direction, log)

    async def _run_fibonacci_series(self, symbol: str, timeframe: str, initial_direction: int, log):
        """Запускает серию Фибоначчи для конкретного сигнала"""
        series_left = int(self.params.get("repeat_count", DEFAULTS["repeat_count"]))
        if series_left <= 0:
            log(f"[{symbol}] 🛑 repeat_count={series_left} — нечего выполнять.")
            return

        next_start_step = 1
        did_place_any_trade = False
        max_steps = int(self.params.get("max_steps", DEFAULTS["max_steps"]))

        while self._running and series_left > 0:
            await self._pause_point()

            if not await self._ensure_anchor_currency():
                continue
            if not await self._ensure_anchor_account_mode():
                continue

            # Проверяем баланс
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0

            min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
            if bal < min_balance:
                log(f"[{symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Ожидание...")
                await self.sleep(2.0)
                continue

            base = float(self.params.get("base_investment", DEFAULTS["base_investment"]))
            min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
            wait_low = float(self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"]))
            account_ccy = self._anchor_ccy

            if max_steps <= 0:
                log(f"[{symbol}] ⚠ max_steps={max_steps} — серию не стартуем.")
                break

            step = next_start_step
            series_direction = initial_direction

            while self._running and step <= max_steps:
                await self._pause_point()

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                # Фибоначчи: ставка = база * число Фибоначчи
                stake = base * _fib(step)

                # Проверяем возраст сигнала
                max_age = self._max_signal_age_seconds()
                if max_age > 0 and self._last_signal_monotonic is not None:
                    age = asyncio.get_running_loop().time() - self._last_signal_monotonic
                    if age > max_age:
                        log(f"[{symbol}] ⚠ Сигнал устарел ({age:.1f}s > {max_age:.0f}s). Прерываем серию.")
                        break

                # Получаем payout
                pct = await get_current_percent(
                    self.http_client,
                    investment=stake,
                    option=symbol,
                    minutes=self._trade_minutes,
                    account_ccy=account_ccy,
                    trade_type=self._trade_type,
                )
                
                if pct is None:
                    self._status("ожидание процента")
                    log(f"[{symbol}] ⚠ Не получили % выплаты. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue
                    
                if pct < min_pct:
                    self._status("ожидание высокого процента")
                    if not self._low_payout_notified:
                        log(f"[{symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём...")
                        self._low_payout_notified = True
                    await self.sleep(wait_low)
                    continue
                    
                if self._low_payout_notified:
                    log(f"[{symbol}] ℹ Работа продолжается (текущий payout = {pct}%)")
                    self._low_payout_notified = False

                # Проверяем баланс
                try:
                    cur_balance, _, _ = await get_balance_info(
                        self.http_client, self.user_id, self.user_hash
                    )
                except Exception:
                    cur_balance = None
                    
                min_floor = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
                if cur_balance is None or (cur_balance - stake) < min_floor:
                    log(f"[{symbol}] 🛑 Сделка {format_amount(stake)} {account_ccy} может опустить баланс ниже "
                        f"{format_amount(min_floor)} {account_ccy}"
                        + ("" if cur_balance is None else f" (текущий {format_amount(cur_balance)} {account_ccy})")
                        + ". Прерываем серию.")
                    break

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                log(f"[{symbol}] step={step} stake={format_amount(stake)} min={self._trade_minutes} "
                    f"side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")

                try:
                    demo_now = await is_demo_account(self.http_client)
                except Exception:
                    demo_now = False
                account_mode = "ДЕМО" if demo_now else "РЕАЛ"

                # Размещаем сделку
                self._status("делает ставку")
                trade_kwargs = {"trade_type": self._trade_type}
                time_arg = self._trade_minutes
                if self._trade_type == "classic":
                    if not self._next_expire_dt:
                        log(f"[{symbol}] ❌ Нет времени экспирации для classic. Пауза и повтор.")
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
                        option=symbol,
                        status=series_direction,
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
                        log(f"[{symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                        await self.sleep(1.0)
                        
                if not trade_id:
                    log(f"[{symbol}] ❌ Не удалось разместить сделку после 4 попыток. Прерываем серию.")
                    break

                did_place_any_trade = True

                # Определяем длительность сделки
                from datetime import datetime
                if self._trade_type == "classic" and self._next_expire_dt is not None:
                    trade_seconds = max(
                        0.0,
                        (self._next_expire_dt - datetime.now(MOSCOW_TZ)).total_seconds(),
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

                # Уведомляем о pending сделке
                placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                if callable(self._on_trade_pending):
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=symbol,
                            timeframe=timeframe,
                            signal_at=self._last_signal_at_str,
                            placed_at=placed_at_str,
                            direction=series_direction,
                            stake=float(stake),
                            percent=int(pct),
                            wait_seconds=float(trade_seconds),
                            account_mode=account_mode,
                            indicator=self._last_indicator,
                            expected_end_ts=expected_end_ts,
                        )
                    except Exception:
                        pass

                self._register_pending_trade(trade_id, symbol, timeframe)

                # Ожидаем результат сделки
                ctx = dict(
                    trade_id=trade_id,
                    wait_seconds=float(wait_seconds),
                    placed_at=placed_at_str,
                    signal_at=self._last_signal_at_str,
                    symbol=symbol,
                    timeframe=timeframe,
                    direction=series_direction,
                    stake=float(stake),
                    percent=int(pct),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                )

                profit = await self._wait_for_trade_result(**ctx)

                if profit is None:
                    log(f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                    step += 1
                elif profit > 0:
                    log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Откат на два шага назад.")
                    next_start_step = max(1, step - 2)
                    break
                elif abs(profit) < 1e-9:
                    log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения.")
                else:
                    log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Переход к следующему числу.")
                    step += 1

                await self.sleep(0.2)

            if not self._running:
                break

            if not did_place_any_trade:
                log(f"[{symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серий осталось: {series_left}.")
            else:
                if step > max_steps:
                    log(f"[{symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии.")
                    next_start_step = 1
                series_left -= 1
                log(f"[{symbol}] ▶ Осталось серий: {series_left}")

                # Если серии закончились, выходим
                if series_left <= 0:
                    break

        log(f"[{symbol}] Завершение серии Фибоначчи")

    def _minutes_from_timeframe(self, tf: str) -> int:
        """Вспомогательный метод для вычисления минут из таймфрейма"""
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

    def _normalize_sprint(self, symbol: str, minutes: int) -> Optional[int]:
        """Вспомогательный метод для нормализации минут спринта"""
        from core.policy import normalize_sprint as ns
        return ns(symbol, minutes)

    async def run(self) -> None:
        """Переопределяем run для использования параллельной архитектуры"""
        self._running = True
        log = self.log or (lambda s: None)

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Режим счёта: {mode_txt}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(f"[{self.symbol}] Баланс: {display} ({format_amount(amount)}), текущая валюта: {cur_ccy}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс при старте: {e}")

        # Инициализируем очередь и задачи (используем родительские методы)
        self._signal_queue = asyncio.Queue()
        self._signal_listener_task = asyncio.create_task(self._signal_listener(self._signal_queue))
        self._signal_processor_task = asyncio.create_task(self._signal_processor(self._signal_queue))

        # Ждем завершения стратегии
        try:
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

        # Завершаем все задачи
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
        if self._signal_processor_task:
            self._signal_processor_task.cancel()

        # Ждем завершения всех активных сделок
        if self._active_trades:
            await asyncio.gather(*list(self._active_trades.values()), return_exceptions=True)
            
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

        self._pending_tasks.clear()
        self._active_trades.clear()
        self._pending_for_status.clear()

        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии Фибоначчи.")
