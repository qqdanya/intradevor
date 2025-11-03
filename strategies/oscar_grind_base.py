from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.money import format_amount
from core.intrade_api_async import is_demo_account

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

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Oscar Grind"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала Oscar Grind")
        
        self._last_indicator = signal_data['indicator']
        self._next_expire_dt = signal_data.get('next_expire')
        signal_received_time = signal_data['timestamp']
       
        # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА С НОВОЙ ЛОГИКОЙ
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        
        if self._trade_type == "classic":
            is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time)
            if not is_valid:
                log(f"[{symbol}] ❌ Сигнал неактуален для classic: {reason}")
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(f"[{symbol}] ❌ Сигнал неактуален для sprint: {reason}")
                return

        await self._run_oscar_grind_series(symbol, timeframe, direction, log, signal_received_time, signal_data)

    async def _run_oscar_grind_series(
        self, 
        symbol: str, 
        timeframe: str, 
        initial_direction: int, 
        log, 
        signal_received_time: datetime, 
        signal_data: dict
    ):
        """Запускает серию Oscar Grind для конкретного сигнала"""
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] repeat_count=0")
            return
            
        base_unit = float(self.params.get("base_investment", 100))
        target_profit = base_unit
        max_steps = int(self.params.get("max_steps", 20))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        double_entry = bool(self.params.get("double_entry", True))
        
        if max_steps <= 0:
            return
            
        step_idx = 0
        cum_profit = 0.0
        stake = base_unit
        series_started = False
        series_direction = initial_direction
        repeat_trade = False
        
        while self._running and step_idx < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue
                
            # ПРОВЕРКА АКТУАЛЬНОСТИ В ПРОЦЕССЕ СЕРИИ
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time)
                if not is_valid:
                    log(f"[{symbol}] ❌ Сигнал стал неактуален в процессе серии: {reason}")
                    return
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(
                    {'timestamp': signal_received_time}, 
                    current_time
                )
                if not is_valid:
                    log(f"[{symbol}] ❌ Сигнал стал неактуален в процессе серии: {reason}")
                    return
                    
            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue
                
            log(f"[{symbol}] step={step_idx + 1} stake={format_amount(stake)} side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")
            
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"
            
            self._status("ставка")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            
            if not trade_id:
                log(f"[{symbol}] Не удалось разместить")
                await asyncio.sleep(2.0)
                continue
                
            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s", trade_seconds)
            
            self._notify_pending_trade(
                trade_id, symbol, timeframe, series_direction, stake, pct, 
                trade_seconds, account_mode, expected_end_ts
            )
            self._register_pending_trade(trade_id, symbol, timeframe)
            
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
                break
                
            need = max(0.0, target_profit - cum_profit)
            next_stake = self._next_stake(
                outcome=outcome, stake=stake, base_unit=base_unit, pct=pct,
                need=need, profit=profit_val, cum_profit=cum_profit, log=log
            )
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
            
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))
                
        if step_idx > 0:
            series_left -= 1
            log(f"[{symbol}] Осталось серий: {series_left}")

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
        expected_end_ts: float
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
