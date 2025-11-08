from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.time_utils import format_local_time
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from strategies.log_messages import (
    repeat_count_empty,
    signal_not_actual_for_placement,
    start_processing,
    series_already_active,
    trade_placement_failed,
    trade_summary,
)

OSCAR_GRIND_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 20,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 300,  # 5 минут
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "double_entry": True,
    "trade_type": "classic",
    "allow_parallel_trades": True,  # Чекбокс "Обрабатывать множество сигналов"
}

class OscarGrindBaseStrategy(BaseTradingStrategy):
    """Oscar Grind: глобальная блокировка при allow_parallel_trades=False"""
    
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
        strategy_name: str = "OscarGrind",
        **kwargs,
    ):
        oscar_params = dict(OSCAR_GRIND_DEFAULTS)
        if params:
            oscar_params.update(params)
            
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=oscar_params,
            strategy_name=strategy_name,
            **kwargs,
        )

        self._active_series: Dict[str, bool] = {}
        self._series_state: Dict[str, dict] = {}

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Oscar Grind"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        trade_key = f"{symbol}_{timeframe}"

        log = self.log or (lambda s: None)
        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            common = getattr(self, "_common", None)
            if common is not None:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        log(start_processing(symbol, "Oscar Grind"))

        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        signal_received_time = signal_data['timestamp']
        self._last_signal_at_str = (
            signal_data.get('signal_time_str')
            or format_local_time(signal_received_time)
        )

        next_expire = signal_data.get('next_expire')
        if next_expire and next_expire.tzinfo is None:
            next_expire = next_expire.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
        self._next_expire_dt = next_expire

        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА С НОВОЙ ЛОГИКОЙ
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

        if self._trade_type == "classic":
            is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                return

        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        series_started = False
        try:
            self._active_series[trade_key] = True
            series_started = True

            updated = await self._run_oscar_grind_series(
                trade_key,
                symbol,
                timeframe,
                direction,
                log,
                series_left,
                signal_received_time,
                signal_data,
            )
            self._set_series_left(trade_key, updated)
        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(f"[{symbol}] Серия Oscar Grind завершена для {timeframe}")

    async def _run_oscar_grind_series(
        self,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        series_left: int,
        signal_received_time: datetime,
        signal_data: dict
    ) -> int:
        """Запускает серию Oscar Grind для конкретного сигнала"""

        base_unit = float(self.params.get("base_investment", 100))
        target_profit = base_unit
        max_steps = int(self.params.get("max_steps", 20))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        double_entry = bool(self.params.get("double_entry", True))

        if max_steps <= 0:
            return series_left

        state = self._series_state.get(trade_key)
        if state:
            step_idx = int(state.get("step_idx", 0))
            cum_profit = float(state.get("cum_profit", 0.0))
            stake = float(state.get("stake", base_unit))
            series_started = bool(state.get("series_started", False))
        else:
            step_idx = 0
            cum_profit = 0.0
            stake = base_unit
            series_started = False
        needs_signal_validation = True
        series_direction = initial_direction
        has_repeated = False
        signal_at_str = signal_data.get('signal_time_str') or format_local_time(signal_received_time)
        series_finished = False

        while self._running and step_idx < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue
                
            # ПРОВЕРКА АКТУАЛЬНОСТИ ТОЛЬКО ДЛЯ ПЕРВОЙ СТАВКИ
            if needs_signal_validation:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data,
                        current_time,
                        for_placement=True,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return series_left
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        {'timestamp': signal_received_time},
                        current_time
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return series_left
                    
            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue
                
            log(trade_summary(symbol, format_amount(stake), self._trade_minutes, series_direction, pct) + f" (step {step_idx + 1})")

            if needs_signal_validation:
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
                    return series_left

                needs_signal_validation = False

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"
            
            self._status("ставка")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            
            if not trade_id:
                log(trade_placement_failed(symbol, "Ждем новый сигнал."))
                # ШАГ НЕ УВЕЛИЧИВАЕМ - ждем новый сигнал для этого же шага
                await self.sleep(2.0)
                return series_left  # Выходим из серии, ждем новый сигнал
            
            series_started = True
                
            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s", trade_seconds)
            
            self._notify_pending_trade(
                trade_id,
                symbol,
                timeframe,
                series_direction,
                stake,
                pct,
                trade_seconds,
                account_mode,
                expected_end_ts,
                signal_at=signal_at_str,
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )
            
            if profit is None:
                profit_val = -float(stake)
                outcome = "loss"
            else:
                profit_val = float(profit)
                outcome = "win" if profit_val > 0 else "refund" if profit_val == 0 else "loss"
                
            if not series_started:
                if outcome == "loss":
                    series_started = True
                    cum_profit += profit_val
                else:
                    stake = base_unit
                    continue
            else:
                cum_profit += profit_val
                
            if cum_profit >= target_profit:
                log(f"[{symbol}] Цель достигнута: {format_amount(cum_profit)}")
                self._series_state.pop(trade_key, None)
                series_finished = True
                break

            need = max(0.0, target_profit - cum_profit)
            next_stake = self._next_stake(
                outcome=outcome, stake=stake, base_unit=base_unit, pct=pct,
                need=need, profit=profit_val, cum_profit=cum_profit, log=log
            )
            stake = float(next_stake)
            step_idx += 1

            if step_idx >= max_steps or not self._running:
                series_finished = True
                self._series_state.pop(trade_key, None)
            else:
                self._series_state[trade_key] = {
                    "stake": stake,
                    "cum_profit": cum_profit,
                    "step_idx": step_idx,
                    "series_started": series_started,
                }

            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))

            should_repeat = double_entry and outcome == "loss" and not has_repeated
            if should_repeat:
                has_repeated = True
                await self.sleep(0.2)
                continue

            await self.sleep(0.2)
            continue

        if series_finished:
            series_left = max(0, series_left - 1)
            log(f"[{symbol}] Осталось серий: {series_left}")
        elif step_idx > 0:
            log(
                f"[{symbol}] Серия продолжается: накоплено "
                f"{format_amount(cum_profit)}/{format_amount(target_profit)}"
            )

        if series_left <= 0 or not self._running or series_finished:
            self._series_state.pop(trade_key, None)

        return series_left

    def stop(self):
        """Останавливает стратегию и очищает состояние серий."""
        super().stop()
        self._series_state.clear()
        self._active_series.clear()

    def is_series_active(self, trade_key: str) -> bool:
        """Возвращает статус активности серии для указанного ключа"""
        return self._active_series.get(trade_key, False)

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
        log
    ) -> float:
        """Определяет следующую ставку - должен быть реализован в дочерних классах"""
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки"""
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0, 
                (self._next_expire_dt - datetime.now(ZoneInfo(MOSCOW_TZ))).total_seconds()
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds
            
        return trade_seconds, expected_end_ts

    def _notify_pending_trade(
        self,
        trade_id: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        trade_seconds: float,
        account_mode: str,
        expected_end_ts: float,
        *,
        signal_at: Optional[str] = None,
    ):
        """Уведомляет о pending сделке"""
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if callable(self._on_trade_pending):
            try:
                self._on_trade_pending(
                    trade_id=trade_id, 
                    symbol=symbol, 
                    timeframe=timeframe,
                    signal_at=signal_at or self._last_signal_at_str,
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
