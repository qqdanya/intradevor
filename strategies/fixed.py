from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_balance_info
from core.payout_provider import get_cached_payout
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
    """Fixed Stake: 1 сделка на актуальный сигнал.
    Если payout низкий — ждём короткими интервалами и подхватываем свежий сигнал,
    а не уходим ждать следующий бар таймфрейма.
    """

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

        self._trades_counter: int = 0
        self._low_payout_notified: bool = False

    # =====================================================================
    # helpers
    # =====================================================================

    def allow_concurrent_trades_per_key(self) -> bool:
        return True

    def _calc_next_candle_from_now(self, timeframe: str) -> datetime:
        now = datetime.now(ZoneInfo(MOSCOW_TZ))
        tf_minutes = _minutes_from_timeframe(timeframe)
        base = now.replace(second=0, microsecond=0)

        total_min = base.hour * 60 + base.minute
        next_total = (total_min // tf_minutes + 1) * tf_minutes

        days_add = next_total // (24 * 60)
        minutes_in_day = next_total % (24 * 60)
        hour = minutes_in_day // 60
        minute = minutes_in_day % 60

        return (base + timedelta(days=days_add)).replace(hour=hour, minute=minute)

    def _apply_signal_context(self, signal_data: dict) -> None:
        """Применяет метаданные сигнала к состоянию стратегии."""
        self._last_signal_ver = signal_data.get("version", self._last_signal_ver)
        self._last_indicator = signal_data.get("indicator", self._last_indicator)

        ts0 = signal_data.get("timestamp")
        if ts0:
            self._last_signal_at_str = signal_data.get("signal_time_str") or format_local_time(ts0)

        ts = signal_data.get("meta", {}).get("next_timestamp") if signal_data.get("meta") else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

    async def _wait_for_new_signal(self, trade_key: str, timeout: float) -> Optional[dict]:
        start = asyncio.get_event_loop().time()
        while self._running and (asyncio.get_event_loop().time() - start) < timeout:
            await self._pause_point()
            common = getattr(self, "_common", None)
            if common is not None:
                sig = common.pop_latest_signal(trade_key)
                if sig:
                    return sig
            await asyncio.sleep(0.5)
        return None

    async def _get_payout_pct(self, symbol: str, stake: float) -> Optional[int]:
        """Возвращает текущий pct или None если недоступно."""
        try:
            pct = await get_cached_payout(
                self.http_client,
                investment=stake,
                option=symbol,
                minutes=self._trade_minutes,
                account_ccy=self._anchor_ccy,
                trade_type=self._trade_type,
            )
        except Exception:
            return None
        return pct

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
        step_label: Optional[str] = None,
    ):
        placed_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        trade_key = self.build_trade_key(symbol, timeframe)

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
                    placed_at=placed_at,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                    series=series_label,
                    step=step_label,
                )
            except Exception:
                pass

    # =====================================================================
    # main
    # =====================================================================

    async def _process_single_signal(self, signal_data: dict):
        log = self.log or (lambda s: None)

        while self._running:
            symbol = signal_data["symbol"]
            timeframe = signal_data["timeframe"]
            direction = int(signal_data["direction"])

            self._maybe_set_auto_minutes(timeframe)
            trade_key = self.build_trade_key(symbol, timeframe)

            log(start_processing(symbol, "Fixed Stake"))
            self._apply_signal_context(signal_data)

            if self._use_any_symbol:
                self.symbol = symbol
            if self._use_any_timeframe:
                self.timeframe = timeframe
                self.params["timeframe"] = timeframe

            # лимит сделок
            max_trades = int(self.params.get("repeat_count", 10))
            if self._trades_counter >= max_trades:
                log(trade_limit_reached(symbol, self._trades_counter, max_trades))
                self._request_stop_when_idle("достигнут лимит сделок")
                return

            stake = float(self.params.get("base_investment", 100))
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            # --- LOW PAYOUT ДО СДЕЛКИ: коротко ждём + подхватываем свежий сигнал ---
            while self._running:
                pct = await self._get_payout_pct(symbol, stake)
                if pct is None:
                    self._status("ожидание процента")
                    log(payout_missing(symbol))
                else:
                    if pct < min_pct:
                        self._status("ожидание высокого процента")
                        if not self._low_payout_notified:
                            log(payout_too_low(symbol, pct, min_pct))
                            self._low_payout_notified = True
                    else:
                        if self._low_payout_notified:
                            log(payout_resumed(symbol, pct))
                            self._low_payout_notified = False
                        break

                # подхватываем самый свежий сигнал (и ОБНОВЛЯЕМ КОНТЕКСТ)
                common = getattr(self, "_common", None)
                if common is not None:
                    newer = common.pop_latest_signal(trade_key)
                    if newer:
                        signal_data = newer
                        symbol = signal_data["symbol"]
                        timeframe = signal_data["timeframe"]
                        direction = int(signal_data["direction"])
                        self._maybe_set_auto_minutes(timeframe)
                        trade_key = self.build_trade_key(symbol, timeframe)
                        self._apply_signal_context(signal_data)

                await asyncio.sleep(wait_low)

            # --- проверка актуальности сигнала (если устарел — ждём новый) ---
            now = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
                if not is_valid:
                    log(signal_not_actual(symbol, "classic", reason))
                    new_signal = await self._wait_for_new_signal(
                        trade_key,
                        timeout=float(self.params.get("signal_timeout_sec", 30.0)),
                    )
                    if not new_signal:
                        return
                    signal_data = new_signal
                    continue
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, now)
                if not is_valid:
                    log(signal_not_actual(symbol, "sprint", reason))
                    new_signal = await self._wait_for_new_signal(
                        trade_key,
                        timeout=float(self.params.get("signal_timeout_sec", 30.0)),
                    )
                    if not new_signal:
                        return
                    signal_data = new_signal
                    continue

            # актуален — размещаем одну сделку
            await self._process_fixed_trade(
                symbol,
                timeframe,
                direction,
                log,
                signal_data["timestamp"],
                signal_data,
            )
            return

    async def _process_fixed_trade(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        log,
        signal_received_time: datetime,
        signal_data: dict,
    ):
        trade_key = self.build_trade_key(symbol, timeframe)

        while self._running:
            signal_at_str = signal_data.get("signal_time_str") or format_local_time(signal_received_time)

            # баланс (с защитой)
            try:
                bal, _, _ = await get_balance_info(self.http_client, self.user_id, self.user_hash)
            except Exception:
                bal = 0.0

            min_balance = float(self.params.get("min_balance", 100))
            if bal < min_balance:
                log(balance_below_min(symbol, format_amount(bal), format_amount(min_balance)))
                return

            stake = float(self.params.get("base_investment", 100))
            min_pct = int(self.params.get("min_percent", 70))

            # (опционально) риск по балансу: после ставки должен остаться min_balance
            if (bal - stake) < min_balance:
                log(
                    stake_risk(
                        symbol,
                        format_amount(stake),
                        self._anchor_ccy,
                        format_amount(min_balance),
                        format_amount(bal),
                    )
                )
                return

            # финальная проверка актуальности
            now = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, now)

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                new_signal = await self._wait_for_new_signal(
                    trade_key,
                    timeout=float(self.params.get("signal_timeout_sec", 30.0)),
                )
                if not new_signal:
                    return
                signal_data = new_signal
                symbol = signal_data["symbol"]
                timeframe = signal_data["timeframe"]
                direction = int(signal_data["direction"])
                signal_received_time = signal_data["timestamp"]
                self._maybe_set_auto_minutes(timeframe)
                trade_key = self.build_trade_key(symbol, timeframe)
                self._apply_signal_context(signal_data)
                continue

            # фактический payout (чтобы в логах/notify был реальный pct)
            pct = await self._get_payout_pct(symbol, stake)
            if pct is None:
                pct = min_pct  # fallback, чтобы не падать на None

            if self._trade_type == "classic":
                self._next_expire_dt = self._calc_next_candle_from_now(timeframe)

            log(trade_summary(symbol, format_amount(stake), self._trade_minutes, direction, int(pct)))

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return

            self._trades_counter += 1

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = float(self.params.get("result_wait_s", trade_seconds))

            self._notify_pending_trade(
                trade_id,
                symbol,
                timeframe,
                direction,
                stake,
                int(pct),
                trade_seconds,
                account_mode,
                expected_end_ts,
                signal_at=signal_at_str,
                series_label=self.format_series_label(trade_key),
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=wait_seconds,
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=stake,
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=self.format_series_label(trade_key),
            )

            if profit is None:
                log(result_unknown(symbol))
            elif profit >= 0:
                log(result_win(symbol, format_amount(profit)))
            else:
                log(result_loss(symbol, format_amount(profit)))

            return

    def stop(self):
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._trades_counter))
        super().stop()
