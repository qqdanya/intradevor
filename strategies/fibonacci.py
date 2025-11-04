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

    async def _run_fibonacci_series():
        pass

async def _wait_for_new_signal(self, trade_key: str, log, symbol: str, timeframe: str, timeout: float = 30.0) -> Optional[dict]:
    """Ожидает новый сигнал в течение timeout секунд"""
    start_time = asyncio.get_event_loop().time()
    
    while self._running and (asyncio.get_event_loop().time() - start_time) < timeout:
        await self._pause_point()
        
        # Проверяем наличие нового сигнала
        if hasattr(self, "_common") and self._common is not None:
            new_signal = self._common.pop_latest_signal(trade_key)
            if new_signal:
                return new_signal
        
        # Ждем немного перед следующей проверкой
        await asyncio.sleep(0.5)
    
    log(f"[{symbol}] ⏰ Таймаут ожидания нового сигнала ({timeout}с)")
    return None

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
