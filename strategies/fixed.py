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
    "max_steps": 10,               # ✅ ВАЖНО: шаги внутри ОДНОЙ серии
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
    """Fixed Stake: одна серия на всю стратегию, шаги = число сделок (max_steps).
    На каждый актуальный сигнал — ОДНА сделка.
    При low payout — коротко ждём и подхватываем более свежий сигнал.
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
    # UI helpers
    # =====================================================================

    def allow_concurrent_trades_per_key(self) -> bool:
        # ✅ FixedStake не должен запускаться параллельно по одному ключу
        # иначе ловишь дубли (как в твоих логах).
        return False

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        # ✅ У FixedStake всегда ровно ОДНА серия
        return "1/1"

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

    def _extract_next_expire_dt(self, signal_data: dict) -> Optional[datetime]:
        nx = signal_data.get("next_expire")
        if isinstance(nx, datetime):
            if nx.tzinfo is None:
                return nx.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
            return nx.astimezone(ZoneInfo(MOSCOW_TZ))

        ts = signal_data.get("meta", {}).get("next_timestamp") if signal_data.get("meta") else None
        if isinstance(ts, datetime):
            return ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts.tzinfo else ts.replace(tzinfo=ZoneInfo(MOSCOW_TZ))

        return None

    def _apply_signal_context(self, signal_data: dict) -> None:
        self._last_signal_ver = signal_data.get("version", self._last_signal_ver)
        self._last_indicator = signal_data.get("indicator", self._last_indicator)

        ts0 = signal_data.get("timestamp")
        if ts0:
            self._last_signal_at_str = signal_data.get("signal_time_str") or format_local_time(ts0)

        next_expire_dt = self._extract_next_expire_dt(signal_data)
        self._next_expire_dt = next_expire_dt

        # ✅ BaseTradingStrategy._is_signal_valid_for_classic требует signal_data["next_expire"]
        if next_expire_dt is not None:
            signal_data["next_expire"] = next_expire_dt

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
        try:
            return await get_cached_payout(
                self.http_client,
                investment=stake,
                option=symbol,
                minutes=self._trade_minutes,
                account_ccy=self._anchor_ccy,
                trade_type=self._trade_type,
            )
        except Exception:
            return None

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
            await self._pause_point()

            symbol = signal_data["symbol"]
            timeframe = signal_data["timeframe"]
            direction = int(signal_data["direction"])

            self._maybe_set_auto_minutes(timeframe)
            trade_key = self.build_trade_key(symbol, timeframe)

            log(start_processing(symbol, "Fixed Stake"))
            self._apply_signal_context(signal_data)

            # ✅ лимит шагов (max_steps), а не repeat_count
            max_steps = int(self.params.get("max_steps", 1))
            if self._trades_counter >= max_steps:
                log(trade_limit_reached(symbol, self._trades_counter, max_steps))
                self._request_stop_when_idle("достигнут лимит шагов (max_steps)")
                return

            stake = float(self.params.get("base_investment", 100))
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            # --- LOW PAYOUT ---
            while self._running:
                await self._pause_point()

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

                # пока ждём payout — можно подхватить более свежий сигнал
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

            # --- проверка актуальности сигнала ---
            now = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
                if not is_valid:
                    log(signal_not_actual(symbol, "classic", reason))
                    new_signal = await self._wait_for_new_signal(trade_key, timeout=float(self.params.get("signal_timeout_sec", 30.0)))
                    if not new_signal:
                        return
                    signal_data = new_signal
                    self._apply_signal_context(signal_data)
                    continue
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, now)
                if not is_valid:
                    log(signal_not_actual(symbol, "sprint", reason))
                    new_signal = await self._wait_for_new_signal(trade_key, timeout=float(self.params.get("signal_timeout_sec", 30.0)))
                    if not new_signal:
                        return
                    signal_data = new_signal
                    self._apply_signal_context(signal_data)
                    continue

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
            await self._pause_point()

            signal_at_str = signal_data.get("signal_time_str") or format_local_time(signal_received_time)

            # баланс
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

            if (bal - stake) < min_balance:
                log(stake_risk(symbol, format_amount(stake), self._anchor_ccy, format_amount(min_balance), format_amount(bal)))
                return

            # финальная актуальность
            now = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, now)

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                new_signal = await self._wait_for_new_signal(trade_key, timeout=float(self.params.get("signal_timeout_sec", 30.0)))
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

            # реальный payout
            pct = await self._get_payout_pct(symbol, stake)
            if pct is None:
                pct = min_pct

            if self._trade_type == "classic":
                self._next_expire_dt = self._calc_next_candle_from_now(timeframe)

            log(trade_summary(symbol, format_amount(stake), self._trade_minutes, direction, int(pct)))

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # ✅ step = текущая сделка из max_steps (считаем ДО инкремента)
            max_steps = int(self.params.get("max_steps", 1))
            step_label = self.format_step_label(self._trades_counter, max_steps)  # 0->1/...

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return

            self._trades_counter += 1

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = float(self.params.get("result_wait_s", trade_seconds))

            series_label = self.format_series_label(trade_key)

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
                series_label=series_label,
                step_label=step_label,  # ✅
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
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
                step_label=step_label,  # ✅
            )

            if profit is None:
                log(result_unknown(symbol))
            elif profit >= 0:
                log(result_win(symbol, format_amount(profit)))
            else:
                log(result_loss(symbol, format_amount(profit)))

            # достигли лимита шагов -> стоп
            max_steps = int(self.params.get("max_steps", 1))
            if self._trades_counter >= max_steps:
                self._request_stop_when_idle("достигнут лимит шагов (max_steps)")

            return

    def stop(self):
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._trades_counter))
        super().stop()

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
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
