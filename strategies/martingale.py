from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.time_utils import format_local_time
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    result_unknown,
)

MARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 5,
    "repeat_count": 10,
    "coefficient": 2.0,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 300,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}

class MartingaleStrategy(BaseTradingStrategy):
    """Стратегия Мартингейла с системой очередей и параллельной обработкой"""

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
        martingale_params = dict(MARTINGALE_DEFAULTS)
        if params:
            martingale_params.update(params)
           
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=martingale_params,
            strategy_name="Martingale",
            **kwargs,
        )

        # Отслеживание активных серий по паре+таймфрейму
        self._active_series: dict[str, bool] = {}
        self._series_remaining: dict[str, int] = {}

    def is_series_active(self, trade_key: str) -> bool:
        """Проверка, выполняется ли серия для указанного ключа."""
        return self._active_series.get(trade_key, False)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Мартингейла"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
       
        log = self.log or (lambda s: None)
        
        # 🔴 ПРОВЕРКА: нет ли активной серии для этой пары+таймфрейма
        trade_key = f"{symbol}_{timeframe}"
        if trade_key in self._active_series and self._active_series[trade_key]:
            log(series_already_active(symbol, timeframe))
            # Передаем сигнал в систему очередей StrategyCommon
            if hasattr(self, '_common'):
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        max_series = int(self.params.get("repeat_count", 10))
        remaining_series = self._series_remaining.get(trade_key)
        if remaining_series is None:
            remaining_series = max_series
            self._series_remaining[trade_key] = remaining_series
        if remaining_series <= 0:
            log(repeat_count_empty(symbol, remaining_series))
            return
        
        series_started = False
        try:
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

            # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА ПЕРЕД НАЧАЛОМ НОВОЙ СЕРИИ
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

            # Помечаем серию как активную только после успешной валидации
            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Мартингейл"))

            # Запускаем серию Мартингейла
            await self._run_martingale_series(trade_key, symbol, timeframe, direction, log, signal_data['timestamp'], signal_data)

        finally:
            if series_started:
                # 🔴 ВАЖНО: Освобождаем серию после завершения
                self._active_series.pop(trade_key, None)
                log(f"[{symbol}] Серия Мартингейла завершена для {timeframe}")

    async def _run_martingale_series(self, trade_key: str, symbol: str, timeframe: str, initial_direction: int, log, signal_received_time: datetime, signal_data: dict):
        """Запускает серию Мартингейла для конкретного сигнала"""
        series_left = self._series_remaining.get(trade_key, int(self.params.get("repeat_count", 10)))
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return
            
        step = 0
        did_place_any_trade = False
        series_direction = initial_direction
        max_steps = int(self.params.get("max_steps", 5))
        
        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue
                
            # ПРОВЕРКА АКТУАЛЬНОСТИ ТОЛЬКО ДЛЯ ПЕРВОЙ СТАВКИ В СЕРИИ
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            
            if not did_place_any_trade:  # ТОЛЬКО перед первой ставкой в новой серии
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        await self.sleep(0.5)
                        continue
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        {'timestamp': signal_received_time},
                        current_time
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        await self.sleep(0.5)
                        continue
                        
            # Рассчитываем ставку
            base_stake = float(self.params.get("base_investment", 100))
            coeff = float(self.params.get("coefficient", 2.0))
            stake = base_stake * (coeff ** step) if step > 0 else base_stake
            
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))
            
            # Проверяем выплату и баланс
            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue
                
            log(f"[{symbol}] step={step} stake={format_amount(stake)} min={self._trade_minutes} "
                f"side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")

            # Финальная проверка актуальности перед размещением сделки
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
                await self.sleep(0.5)
                continue

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
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return  # ПРОПУСК СИГНАЛА
                
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
            
            # Обрабатываем результат
            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True))
                step += 1
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(f"[{symbol}] 🗑 Удалено сигналов из очередей после LOSS: {removed}")
            elif profit > 0:
                log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Серия завершена.")
                break
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без увеличения.")
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(f"[{symbol}] 🗑 Удалено сигналов из очередей после PUSH: {removed}")
            else:
                log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Увеличиваем ставку.")
                step += 1  # Продолжаем с тем же направлением и исходным сигналом
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(f"[{symbol}] 🗑 Удалено сигналов из очередей после LOSS: {removed}")

            await self.sleep(0.2)
            
            # Обновляем время экспирации для classic
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(
                    minutes=_minutes_from_timeframe(timeframe)
                )
                
        if did_place_any_trade:
            if step >= max_steps:
                log(f"[{symbol}] 🛑 Достигнут лимит шагов ({max_steps}).")
            series_left = max(0, series_left - 1)
            self._series_remaining[trade_key] = series_left
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

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

    def stop(self):
        """Остановка стратегии с очисткой активных серий"""
        super().stop()
        self._active_series.clear()
        self._series_remaining.clear()

    def update_params(self, **params):
        """Обновление параметров стратегии"""
        super().update_params(**params)
        if "repeat_count" in params:
            self._series_remaining.clear()
