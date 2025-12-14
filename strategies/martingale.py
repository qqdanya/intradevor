from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from strategies.strategy_helpers import (
    calc_next_candle_from_now,
    extract_next_expire_dt,
    is_payout_low_now,
    update_signal_context,
    wait_for_new_signal,
)
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
    series_completed,
    trade_step,
    win_with_series_finish,
    push_repeat,
    loss_with_increase,
    steps_limit_reached,
    series_remaining,
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
    """Стратегия Мартингейла с системой очередей и параллельной обработкой.
    Важно:
      - Classic: экспирация = следующая свеча от "сейчас"
      - Sprint: окно актуальности расширяется внутри серии по consecutive_non_win
      - Если сигнал устарел для размещения — НЕ завершаем серию, ждём новый сигнал
      - При низком payout ДО старта серии: НЕ кладём в локальную очередь (её нет),
        а ждём восстановления payout короткими интервалами и подхватываем свежий сигнал.
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

        self._active_series: dict[str, bool] = {}
        self._series_remaining: dict[str, int] = {}

    # =====================================================================
    # ожидание нового сигнала / обновление контекста
    # =====================================================================

    def _update_signal_context_in_series(
        self,
        *,
        new_signal: dict,
    ) -> tuple[dict, datetime, int, str]:
        """
        Обновляет локальный контекст серии по новому сигналу.
        Возвращает: (signal_data, signal_received_time, series_direction, signal_at_str)
        """
        return update_signal_context(self, new_signal)

    # =====================================================================
    # Sprint расширенная валидация для мартингейла
    # =====================================================================

    def _is_sprint_signal_valid_for_martingale(
        self,
        signal_data: dict,
        now_dt: datetime,
        *,
        consecutive_non_win: int,
    ) -> tuple[bool, str]:
        raw_ts = signal_data.get("timestamp")
        if raw_ts is None:
            return False, "нет timestamp у сигнала"

        if raw_ts.tzinfo is None:
            signal_ts = raw_ts.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
        else:
            signal_ts = raw_ts.astimezone(ZoneInfo(MOSCOW_TZ))

        trade_sec = float(self._trade_minutes) * 60.0
        candles = max(1, 1 + consecutive_non_win)
        max_age = candles * trade_sec

        age = (now_dt - signal_ts).total_seconds()
        if age > max_age:
            return False, f"сигналу {age:.1f}с > {max_age:.0f}с"

        return True, "актуален"

    # =====================================================================
    # Public
    # =====================================================================

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    async def _process_single_signal(self, signal_data: dict):
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = signal_data["direction"]

        self._maybe_set_auto_minutes(timeframe)

        log = self.log or (lambda s: None)
        trade_key = self.build_trade_key(symbol, timeframe)

        # активная серия -> в StrategyCommon
        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            common = getattr(self, "_common", None)
            if common is not None:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        # === LOW PAYOUT ДО СТАРТА СЕРИИ ===
        # Не уходим в "ждать новый сигнал 15м/1ч". Ждём восстановления payout коротко,
        # и по пути подхватываем самый свежий сигнал из StrategyCommon.
        while self._running and await is_payout_low_now(self, symbol):
            await self._pause_point()

            common = getattr(self, "_common", None)
            if common is not None:
                newer = common.pop_latest_signal(trade_key)
                if newer:
                    signal_data = newer
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    direction = signal_data["direction"]
                    self._maybe_set_auto_minutes(timeframe)
                    trade_key = self.build_trade_key(symbol, timeframe)

            await asyncio.sleep(float(self.params.get("wait_on_low_percent", 1)))

        # repeat_count
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
            # метаданные сигнала
            self._last_signal_ver = signal_data["version"]
            self._last_indicator = signal_data["indicator"]
            self._last_signal_at_str = format_local_time(signal_data["timestamp"])
            ts = signal_data.get("meta", {}).get("next_timestamp")
            self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

            if self._use_any_symbol:
                self.symbol = symbol
            if self._use_any_timeframe:
                self.timeframe = timeframe
                self.params["timeframe"] = self.timeframe

            try:
                self._last_signal_monotonic = asyncio.get_running_loop().time()
            except RuntimeError:
                self._last_signal_monotonic = None

            # стартовая валидация (если невалидно — ждём новый сигнал и пробуем снова)
            while self._running:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                    if not is_valid:
                        log(signal_not_actual(symbol, "classic", reason))
                        timeout = float(self.params.get("signal_timeout_sec", 30.0))
                        new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                        if not new_signal:
                            return
                        signal_data, _, direction, _ = self._update_signal_context_in_series(new_signal=new_signal)
                        symbol = signal_data["symbol"]
                        timeframe = signal_data["timeframe"]
                        self._maybe_set_auto_minutes(timeframe)
                        continue
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
                    if not is_valid:
                        log(signal_not_actual(symbol, "sprint", reason))
                        timeout = float(self.params.get("signal_timeout_sec", 30.0))
                        new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                        if not new_signal:
                            return
                        signal_data, _, direction, _ = self._update_signal_context_in_series(new_signal=new_signal)
                        symbol = signal_data["symbol"]
                        timeframe = signal_data["timeframe"]
                        self._maybe_set_auto_minutes(timeframe)
                        continue
                break

            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Мартингейл"))

            await self._run_martingale_series(
                trade_key,
                symbol,
                timeframe,
                direction,
                log,
                signal_data["timestamp"],
                signal_data,
            )
        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(series_completed(symbol, timeframe, "Мартингейл"))

    async def _run_martingale_series(
        self,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        signal_received_time: datetime,
        signal_data: dict,
    ):
        series_left = self._series_remaining.get(trade_key, int(self.params.get("repeat_count", 10)))
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        step = 0
        did_place_any_trade = False
        consecutive_non_win = 0

        series_direction = int(initial_direction)
        signal_at_str = signal_data.get("signal_time_str") or format_local_time(signal_received_time)
        max_steps = int(self.params.get("max_steps", 5))
        series_label = self.format_series_label(trade_key, series_left=series_left)

        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            # 1) для первой ставки — проверка (если невалидно: ждём новый сигнал, серию НЕ завершаем)
            if not did_place_any_trade:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint({"timestamp": signal_received_time}, current_time)

                if not is_valid:
                    log(signal_not_actual_for_placement(symbol, reason))
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                    if not new_signal:
                        return
                    signal_data, signal_received_time, series_direction, signal_at_str = \
                        self._update_signal_context_in_series(new_signal=new_signal)
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    self._maybe_set_auto_minutes(timeframe)
                    continue

            # ставка
            base_stake = float(self.params.get("base_investment", 100))
            coeff = float(self.params.get("coefficient", 2.0))
            stake = base_stake * (coeff ** step) if step > 0 else base_stake

            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            pct, _ = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(trade_step(symbol, step, format_amount(stake), self._trade_minutes, series_direction, pct))

            # 2) финальная проверка перед сделкой (если невалидно: ждём новый сигнал и продолжаем)
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                original_max_age = self.params.get("classic_signal_max_age_sec", 170.0)

                tf_minutes = _minutes_from_timeframe(timeframe)
                candles = max(1, 1 + consecutive_non_win)
                extended_max_age = candles * tf_minutes * 60
                self.params["classic_signal_max_age_sec"] = extended_max_age

                for_placement_flag = consecutive_non_win == 0
                try:
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data, current_time, for_placement=for_placement_flag
                    )
                finally:
                    self.params["classic_signal_max_age_sec"] = original_max_age
            else:
                is_valid, reason = self._is_sprint_signal_valid_for_martingale(
                    signal_data, current_time, consecutive_non_win=consecutive_non_win
                )

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_signal:
                    return
                signal_data, signal_received_time, series_direction, signal_at_str = \
                    self._update_signal_context_in_series(new_signal=new_signal)
                symbol = signal_data["symbol"]
                timeframe = signal_data["timeframe"]
                self._maybe_set_auto_minutes(timeframe)
                continue

            # classic: экспирация = следующая свеча от "сейчас"
            if self._trade_type == "classic":
                self._next_expire_dt = calc_next_candle_from_now(timeframe)

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return

            did_place_any_trade = True

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s")
            wait_seconds = trade_seconds if wait_seconds is None else float(wait_seconds)

            step_label = self.format_step_label(step, max_steps)
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
                series_label=series_label,
                step_label=step_label,
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
                series_label=series_label,
                step_label=step_label,
            )

            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True))
                step += 1
                consecutive_non_win += 1
            elif profit > 0:
                log(win_with_series_finish(symbol, format_amount(profit)))
                break
            elif abs(profit) < 1e-9:
                log(push_repeat(symbol))
                consecutive_non_win += 1
            else:
                log(loss_with_increase(symbol, format_amount(profit)))
                step += 1
                consecutive_non_win += 1

            await self.sleep(0.2)

        if did_place_any_trade:
            if step >= max_steps:
                log(steps_limit_reached(symbol, max_steps))
            series_left = max(0, series_left - 1)
            self._series_remaining[trade_key] = series_left
            log(series_remaining(symbol, series_left))
            self._check_all_series_completed(self._series_remaining)

    # =====================================================================
    # СЛУЖЕБНЫЕ
    # =====================================================================

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

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        if series_left is None:
            series_left = self._series_remaining.get(trade_key)
        return super().format_series_label(trade_key, series_left=series_left)

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

    def stop(self):
        super().stop()
        self._active_series.clear()
        self._series_remaining.clear()

    def update_params(self, **params):
        super().update_params(**params)
        if "repeat_count" in params:
            self._series_remaining.clear()
