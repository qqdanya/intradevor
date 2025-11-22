from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_balance_info, get_current_percent, place_trade, check_trade_result
from core.time_utils import format_local_time
from strategies.log_messages import (
    start_processing,
    signal_not_actual,
    signal_not_actual_for_placement,
    trade_placement_failed,
    payout_missing,
    payout_too_low,
    payout_resumed,
    stake_risk,
    trade_summary,
    result_unknown,
    result_win,
    result_loss,
    balance_below_min,
    trade_limit_reached,
    fixed_stake_stopped,
)

FIXED_DEFAULTS = {
    "base_investment": 100,
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

class FixedStakeStrategy(BaseTradingStrategy):
    """Стратегия с фиксированной ставкой"""

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
        fixed_params = dict(FIXED_DEFAULTS)
        if params:
            fixed_params.update(params)
           
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=fixed_params,
            strategy_name="FixedStake",
            **kwargs,
        )
       
        # Специфичные атрибуты для Fixed Stake
        self._trades_counter: int = 0  # Счетчик сделок

    def allow_concurrent_trades_per_key(self) -> bool:
        """Fixed Stake допускает несколько одновременных сделок по одной паре."""
        return True

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для фиксированной ставки"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
       
        log = self.log or (lambda s: None)
        log(start_processing(symbol, "Fixed Stake"))
       
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
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not is_valid:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        # Проверяем лимит сделок
        max_trades = int(self.params.get("repeat_count", 10))
        if self._trades_counter >= max_trades:
            if not self._stop_when_idle_requested:
                log(trade_limit_reached(symbol, self._trades_counter, max_trades))
                self._status("достигнут лимит сделок")
            self._request_stop_when_idle("достигнут лимит сделок")
            return

        # Запускаем обработку сделки с фиксированной ставкой
        await self._process_fixed_trade(symbol, timeframe, direction, log, signal_data['timestamp'], signal_data)

    async def _process_fixed_trade(self, symbol: str, timeframe: str, direction: int, log, signal_received_time: datetime, signal_data: dict):
        """Обрабатывает одну сделку с фиксированной ставкой"""
        signal_at_str = signal_data.get('signal_time_str') or format_local_time(signal_received_time)
        trade_key = f"{symbol}_{timeframe}"
        # Проверяем баланс
        try:
            bal, _, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            bal = 0.0
            
        min_balance = float(self.params.get("min_balance", 100))
        if bal < min_balance:
            log(
                balance_below_min(
                    symbol, format_amount(bal), format_amount(min_balance)
                )
            )
            return

        stake = float(self.params.get("base_investment", 100))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        account_ccy = self._anchor_ccy

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
            log(payout_missing(symbol))
            return

        if pct < min_pct:
            self._status("ожидание высокого процента")
            if not self._low_payout_notified:
                log(payout_too_low(symbol, pct, min_pct))
                self._low_payout_notified = True
            return

        if self._low_payout_notified:
            log(payout_resumed(symbol, pct))
            self._low_payout_notified = False

        # Проверяем баланс для конкретной сделки
        try:
            cur_balance, _, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            cur_balance = None
           
        min_floor = float(self.params.get("min_balance", 100))
        if cur_balance is None or (cur_balance - stake) < min_floor:
            current_display = format_amount(cur_balance) if cur_balance is not None else None
            log(
                stake_risk(
                    symbol,
                    format_amount(stake),
                    account_ccy,
                    format_amount(min_floor),
                    current_display,
                )
            )
            return

        if not await self.ensure_account_conditions():
            return

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
            return

        log(trade_summary(symbol, format_amount(stake), self._trade_minutes, direction, pct))

        try:
            demo_now = await is_demo_account(self.http_client)
        except Exception:
            demo_now = False
        account_mode = "ДЕМО" if demo_now else "РЕАЛ"

        # Размещаем сделку
        self._status("делает ставку")
        trade_id = await self.place_trade_with_retry(
            symbol, direction, stake, self._anchor_ccy
        )
               
        if not trade_id:
            log(trade_placement_failed(symbol, "Пропускаем сигнал."))
            return  # ПРОПУСКАЕМ СИГНАЛ ВМЕСТО УВЕЛИЧЕНИЯ СЧЕТЧИКА

        # Увеличиваем счетчик сделок ТОЛЬКО при успешном размещении
        self._trades_counter += 1

        # Определяем длительность сделки
        trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
        wait_seconds = self.params.get("result_wait_s")
        if wait_seconds is None:
            wait_seconds = trade_seconds
        else:
            wait_seconds = float(wait_seconds)

        # Уведомляем о pending сделке
        series_label = self.format_series_label(trade_key)
        self._notify_pending_trade(
            trade_id,
            symbol,
            timeframe,
            direction,
            stake,
            pct,
            trade_seconds,
            account_mode,
            expected_end_ts,
            signal_at=signal_at_str,
            series_label=series_label,
        )
        self._register_pending_trade(trade_id, symbol, timeframe)

        # Ожидаем результат сделки
        profit = await self.wait_for_trade_result(
            trade_id=trade_id,
            wait_seconds=float(wait_seconds),
            placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            signal_at=signal_at_str,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            stake=float(stake),
            percent=int(pct),
            account_mode=account_mode,
            indicator=self._last_indicator,
            series_label=series_label,
        )

        # Логируем результат
        if profit is None:
            log(result_unknown(symbol))
        elif profit >= 0:
            log(result_win(symbol, f"Результат: {format_amount(profit)}"))
        else:
            log(result_loss(symbol, f"Результат: {format_amount(profit)}"))

        # Обновляем статус с оставшимися сделками
        max_trades = int(self.params.get("repeat_count", 10))
        remaining = max_trades - self._trades_counter
        if remaining > 0:
            self._status(f"сделок осталось: {remaining}")
        else:
            self._status("достигнут лимит сделок")
            self._request_stop_when_idle("достигнут лимит сделок")

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
        series_label: Optional[str] = None,
    ):
        """Уведомляет о pending сделке"""
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        trade_key = f"{symbol}_{timeframe}"
        if series_label is None:
            series_label = self.format_series_label(trade_key)
        self._set_planned_stake(trade_key, stake)
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
                    series=series_label,
                )
            except Exception:
                pass

    def format_series_label(
        self, trade_key: str, *, series_left: int | None = None
    ) -> str | None:
        try:
            total = int(self.params.get("repeat_count", 0))
        except Exception:
            total = 0
        if total <= 0:
            return None
        current = max(1, min(total, int(self._trades_counter)))
        return f"{current}/{total}"

    def stop(self):
        """Остановка стратегии"""
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._trades_counter))
        super().stop()

    def update_params(self, **params):
        """Обновление параметров с дополнительной логикой"""
        super().update_params(**params)
       
        # Можно добавить специфичную логику для Fixed Stake при обновлении параметров
        if "repeat_count" in params:
            max_trades = int(params["repeat_count"])
            remaining = max_trades - self._trades_counter
            if remaining > 0:
                self._status(f"сделок осталось: {remaining}")
            else:
                self._status("достигнут лимит сделок")
                self._request_stop_when_idle("достигнут лимит сделок")
