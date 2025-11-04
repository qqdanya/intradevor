from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_balance_info
from core.time_utils import format_local_time
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    trade_summary,
    result_unknown,
)

FIBONACCI_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 5,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}

def _fib(n: int) -> int:
    """Возвращает n-е число Фибоначчи (1-indexed)."""
    if n <= 0:
        return 1
    seq = [1, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq[n - 1]

class FibonacciStrategy(BaseTradingStrategy):
    """Стратегия Фибоначчи (управление ставками по последовательности Фибоначчи)"""

    def __init__(
        self,
        http_client,
        user_id: str,
        user_hash: str,
        symbol: str,
        log_callback=None,
        *,
        timeframe: str = "M1",
        params: Optional[dict] = None,
        **kwargs,
    ):
        # Объединяем параметры по умолчанию
        fibonacci_params = dict(FIBONACCI_DEFAULTS)
        if params:
            fibonacci_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=fibonacci_params,
            strategy_name="Fibonacci",
            **kwargs,
        )

        # Отслеживание активных серий по паре+таймфрейму
        self._active_series: dict[str, bool] = {}

    def is_series_active(self, trade_key: str) -> bool:
        """Показывает, выполняется ли серия для указанного ключа."""
        return self._active_series.get(trade_key, False)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для стратегии Фибоначчи"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        trade_key = f"{symbol}_{timeframe}"

        log = self.log or (lambda s: None)

        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            if hasattr(self, '_common'):
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        # Обновляем информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = format_local_time(signal_data['timestamp'])

        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        # Обновляем символ и таймфрейм если используются "все"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА (перед стартом серий)
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

        if self._trade_type == "classic":
            is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not is_valid:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        series_started = False
        try:
            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Фибоначчи"))

            # Запускаем серии Фибоначчи
            updated = await self._run_fibonacci_series(
                trade_key,
                symbol,
                timeframe,
                direction,
                log,
                series_left,
                signal_data['timestamp'],
                signal_data,
            )
            self._set_series_left(trade_key, updated)
        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(f"[{symbol}] Серия Фибоначчи завершена для {timeframe}")

    async def _run_fibonacci_series(
        self,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        series_left: int,
        signal_received_time: datetime,
        signal_data: dict,
    ) -> int:
        """Запускает (несколько) серий Фибоначчи для конкретного сигнала"""
        max_steps = int(self.params.get("max_steps", 5))
    
        while self._running and series_left > 0:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue
    
            # Проверяем баланс
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
            except Exception:
                bal = 0.0
    
            min_balance = float(self.params.get("min_balance", 100))
            if bal < min_balance:
                log(f"[{symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Ожидание...")
                await self.sleep(2.0)
                continue
    
            base = float(self.params.get("base_investment", 100))
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))
    
            if max_steps <= 0:
                log(f"[{symbol}] ⚠ max_steps={max_steps} — серию не стартуем.")
                break
    
            # ------------------------------------------------------------------
            # FIX: Полный сброс состояния ДЛЯ НОВОЙ СЕРИИ
            next_start_step = 1
            did_place_any_trade = False
            force_validate_signal = False
            reuse_previous_signal = False
            step = next_start_step
            series_direction = initial_direction
            # ------------------------------------------------------------------
    
            # ВНУТРЕННИЙ ЦИКЛ ШАГОВ ФИБОНАЧЧИ ВНУТРИ ОДНОЙ СЕРИИ
            while self._running and step <= max_steps:
                await self._pause_point()
                if not await self.ensure_account_conditions():
                    continue
    
                # Подхватываем новый сигнал (если есть) — актуализируем направление/таймфрейм
                new_signal = None
                if not reuse_previous_signal and hasattr(self, "_common") and self._common is not None:
                    new_signal = self._common.pop_latest_signal(trade_key)
    
                if new_signal:
                    new_direction = new_signal.get('direction')
                    if new_direction is None:
                        log(f"[{symbol}] ⚠ Новый сигнал без направления — пропускаем обновление.")
                    else:
                        symbol = new_signal.get('symbol', symbol)
                        timeframe = new_signal.get('timeframe', timeframe)
                        signal_data = new_signal
                        initial_direction = new_direction
                        series_direction = new_direction
                        signal_received_time = new_signal['timestamp']
                        self._last_signal_ver = new_signal.get('version', self._last_signal_ver)
                        indicator = new_signal.get('indicator')
                        if indicator is not None:
                            self._last_indicator = indicator
                        self._last_signal_at_str = format_local_time(signal_received_time)
    
                        next_expire = new_signal.get('next_expire')
                        if not next_expire:
                            meta = new_signal.get('meta') or {}
                            next_raw = meta.get('next_timestamp')
                            if next_raw is not None:
                                if hasattr(next_raw, 'astimezone'):
                                    next_expire = next_raw.astimezone(ZoneInfo(MOSCOW_TZ))
                                else:
                                    next_expire = next_raw
                        self._next_expire_dt = next_expire
    
                        if self._use_any_symbol:
                            self.symbol = symbol
                        if self._use_any_timeframe:
                            self.timeframe = timeframe
                            self.params["timeframe"] = self.timeframe
    
                        log(f"[{symbol}] 🔄 Обновление серии Фибоначчи по новому сигналу.")
                        force_validate_signal = True
                        reuse_previous_signal = False
    
                # ПРОВЕРКА АКТУАЛЬНОСТИ ПЕРЕД РАЗМЕЩЕНИЕМ СДЕЛКИ
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                need_validate = (not did_place_any_trade) or force_validate_signal
                validate_for_placement = need_validate
    
                if need_validate:
                    if self._trade_type == "classic":
                        is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                        if not is_valid:
                            log(signal_not_actual_for_placement(symbol, reason))
                            # ВМЕСТО ЗАВЕРШЕНИЯ СЕРИИ - ЖДЕМ НОВЫЙ СИГНАЛ
                            log(f"[{symbol}] ⏳ Ожидание нового сигнала...")
                            await asyncio.sleep(1.0)  # Короткая пауза перед следующей проверкой
                            continue  # Продолжаем цикл, ожидая новый сигнал
                    else:
                        is_valid, reason = self._is_signal_valid_for_sprint(
                            {'timestamp': signal_received_time},
                            current_time
                        )
                        if not is_valid:
                            log(signal_not_actual_for_placement(symbol, reason))
                            # ВМЕСТО ЗАВЕРШЕНИЯ СЕРИИ - ЖДЕМ НОВЫЙ СИГНАЛ
                            log(f"[{symbol}] ⏳ Ожидание нового сигнала...")
                            await asyncio.sleep(1.0)
                            continue  # Продолжаем цикл, ожидая новый сигнал
    
                force_validate_signal = False
    
                # Фибоначчи: ставка = база * число Фибоначчи
                stake = base * _fib(step)
    
                # Проверяем выплату и баланс
                pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
                if pct is None:
                    continue
    
                log(trade_summary(symbol, format_amount(stake), self._trade_minutes, series_direction, pct) + f" (Fib#{step})")
    
                # Финальная проверка актуальности (дублирующая защита)
                if validate_for_placement:
                    current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                    if self._trade_type == "classic":
                        is_valid, reason = self._is_signal_valid_for_classic(
                            signal_data,
                            current_time,
                            for_placement=True,
                        )
                    else:
                        sprint_payload = signal_data
                        if not sprint_payload.get('timestamp'):
                            sprint_payload = {'timestamp': signal_received_time}
                        is_valid, reason = self._is_signal_valid_for_sprint(
                            sprint_payload,
                            current_time,
                        )
    
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        # ВМЕСТО ЗАВЕРШЕНИЯ СЕРИИ - ЖДЕМ НОВЫЙ СИГНАЛ
                        log(f"[{symbol}] ⏳ Ожидание нового сигнала...")
                        await asyncio.sleep(1.0)
                        continue  # Продолжаем цикл, ожидая новый сигнал
    
                # Определяем режим аккаунта
                try:
                    demo_now = await is_demo_account(self.http_client)
                except Exception:
                    demo_now = False
                account_mode = "ДЕМО" if demo_now else "РЕАЛ"
    
                # Размещаем сделку
                self._status("делает ставку")
                trade_id = await self.place_trade_with_retry(
                    symbol, series_direction, stake, self._anchor_ccy
                )
    
                if not trade_id:
                    log(trade_placement_failed(symbol, "Ждем новый сигнал."))
                    break  # выходим из внутреннего цикла, шаг не увеличиваем
    
                did_place_any_trade = True
    
                # Определяем длительность сделки
                trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
                wait_seconds = self.params.get("result_wait_s")
                if wait_seconds is None:
                    wait_seconds = trade_seconds
                else:
                    wait_seconds = float(wait_seconds)
    
                # Уведомляем о pending сделке
                self._notify_pending_trade(
                    trade_id, symbol, timeframe, series_direction, stake, pct,
                    trade_seconds, account_mode, expected_end_ts
                )
                self._register_pending_trade(trade_id, symbol, timeframe)
    
                # Ожидаем результат сделки
                profit = await self.wait_for_trade_result(
                    trade_id=trade_id,
                    wait_seconds=float(wait_seconds),
                    placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    signal_at=self._last_signal_at_str,
                    symbol=symbol,
                    timeframe=timeframe,
                    direction=series_direction,
                    stake=float(stake),
                    percent=int(pct),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                )
    
                # Обрабатываем результат по логике Фибоначчи
                if profit is None:
                    log(result_unknown(symbol, treat_as_loss=True))
                    step += 1
                    reuse_previous_signal = False
                elif profit > 0:
                    log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Серия завершена.")
                    break
                elif abs(profit) < 1e-9:
                    log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения.")
                    reuse_previous_signal = True
                else:
                    log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Переход к следующему числу Фибоначчи.")
                    step += 1
                    reuse_previous_signal = False
    
                await self.sleep(0.2)
    
                # Обновляем время экспирации для classic
                if self._trade_type == "classic" and self._next_expire_dt is not None:
                    self._next_expire_dt += timedelta(
                        minutes=_minutes_from_timeframe(timeframe)
                    )
    
            if not self._running:
                break
    
            if not did_place_any_trade:
                log(f"[{symbol}] ℹ Серия завершена без сделок (max_steps={max_steps} или условия не выполнились). "
                    f"Серий осталось: {series_left}.")
            else:
                if step > max_steps:
                    log(f"[{symbol}] 🛑 Достигнут лимит шагов ({max_steps}). Переход к новой серии.")
    
                # Переход к НОВОЙ СЕРИИ
                series_left -= 1
                log(f"[{symbol}] ▶ Осталось серий: {series_left}")
    
                if series_left <= 0:
                    break
    
        log(f"[{symbol}] Завершение серии Фибоначчи")
        return series_left

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки"""
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - datetime.now(ZoneInfo(MOSCOW_TZ))).total_seconds(),
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds

        return trade_seconds, expected_end_ts

    def _notify_pending_trade(
        self, trade_id: str, symbol: str, timeframe: str, direction: int,
        stake: float, percent: int, trade_seconds: float,
        account_mode: str, expected_end_ts: float
    ):
        """Уведомляет о pending сделке"""
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if callable(self._on_trade_pending):
            try:
                self._on_trade_pending(
                    trade_id=trade_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_at=self._last_signal_at_str,
                    placed_at=placed_at_str,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                )
            except Exception:
                pass
