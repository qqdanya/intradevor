from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from core.time_utils import format_local_time

ANTI_MARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 3,
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

class AntiMartingaleStrategy(BaseTradingStrategy):
    """Стратегия Антимартингейла (увеличиваем ставку после выигрыша)"""

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
        anti_martingale_params = dict(ANTI_MARTINGALE_DEFAULTS)
        if params:
            anti_martingale_params.update(params)
           
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=anti_martingale_params,
            strategy_name="AntiMartingale",
            **kwargs,
        )

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Антимартингейла"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        trade_key = f"{symbol}_{timeframe}"
       
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала (Антимартингейл)")
       
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

        # ПРОВЕРКА АКТУАЛЬНОСТИ СИГНАЛА С НОВОЙ ЛОГИКОЙ
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        
        if self._trade_type == "classic":
            is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not is_valid:
                log(f"[{symbol}] ❌ Сигнал неактуален для classic: {reason}")
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(f"[{symbol}] ❌ Сигнал неактуален для sprint: {reason}")
                return

        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(f"[{symbol}] 🛑 repeat_count={series_left} — нечего выполнять.")
            return

        # Запускаем серию Антимартингейла для этого сигнала
        updated = await self._run_antimartingale_series(
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

    async def _run_antimartingale_series(
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
        """Запускает серию Антимартингейла для конкретного сигнала"""

        step = 0
        did_place_any_trade = False
        max_steps = int(self.params.get("max_steps", 3))
        base_stake = float(self.params.get("base_investment", 100))
        current_stake = base_stake
        
        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue
                
            # ПРОВЕРКА АКТУАЛЬНОСТИ ТОЛЬКО ДЛЯ ПЕРВОЙ СТАВКИ
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            
            if not did_place_any_trade:  # ТОЛЬКО перед первой ставкой
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                    if not is_valid:
                        log(f"[{symbol}] ❌ Сигнал неактуален для размещения: {reason}")
                        return series_left
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
                    if not is_valid:
                        log(f"[{symbol}] ❌ Сигнал неактуален для размещения: {reason}")
                        return series_left
                        
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))
            
            # Проверяем выплату и баланс
            pct, balance = await self.check_payout_and_balance(symbol, current_stake, min_pct, wait_low)
            if pct is None:
                continue
                
            log(f"[{symbol}] step={step} stake={format_amount(current_stake)} min={self._trade_minutes} "
                f"side={'UP' if initial_direction == 1 else 'DOWN'} payout={pct}%")
                
            # Определяем режим аккаунта
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"
            
            # Размещаем сделку
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(
                symbol, initial_direction, current_stake, self._anchor_ccy
            )
                   
            if not trade_id:
                log(f"[{symbol}] ❌ Не удалось разместить сделку. Ждем новый сигнал.")
                return series_left  # ВЫХОДИМ ИЗ СЕРИИ, ЖДЕМ НОВЫЙ СИГНАЛ

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
                trade_id, symbol, timeframe, initial_direction, current_stake, pct,
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
                direction=initial_direction,
                stake=float(current_stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )
            
            # Обрабатываем результат по логике Антимартингейла
            if profit is None:
                log(f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                break
            elif profit > 0:
                log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Увеличиваем ставку.")
                # Антимартингейл: увеличиваем ставку на размер выигрыша
                current_stake += float(profit)
                removed = self._common.discard_signals_for(trade_key)
                if removed:
                    log(f"[{symbol}] 🧹 Удалено сигналов из очереди: {removed}")
                step += 1
                if step >= max_steps:
                    log(f"[{symbol}] 🎯 Достигнут лимит шагов ({max_steps}).")
                    break
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения ставки.")
                removed = self._common.discard_signals_for(trade_key)
                if removed:
                    log(f"[{symbol}] 🧹 Удалено сигналов из очереди: {removed}")
            else:
                log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Серия завершена.")
                break
                
            await self.sleep(0.2)
            
            # Обновляем время экспирации для classic
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(
                    minutes=_minutes_from_timeframe(timeframe)
                )
                
        if did_place_any_trade:
            series_left -= 1
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

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
