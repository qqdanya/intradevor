from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.time_utils import format_local_time
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_current_percent
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
    win_with_parlay,
    push_repeat_same_stake,
    loss_series_finish,
    steps_limit_reached,
    series_remaining,
)


ANTIMARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 3,
    "repeat_count": 10,
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


class AntiMartingaleStrategy(BaseTradingStrategy):
    """
    Антимартингейл (парлей) под нашу архитектуру.

    Правила:
      - Увеличиваем ставку ПОСЛЕ WIN на размер фактического выигрыша (парлей).
      - После WIN или PUSH НЕ повторяем ставку без нового сигнала:
        берём из очереди StrategyCommon или ждём новый.
      - При LOSS или UNKNOWN:
          * если НЕ было ни одного WIN в этой серии — серия считается НЕ начатой,
            repeat_count не тратим;
          * если был хотя бы один WIN — серия считается завершённой.
      - Если сигнал "неактуален для размещения" — СЕРИЮ НЕ ЗАВЕРШАЕМ:
        ждём новый сигнал и продолжаем с прежними параметрами.
      - При низком payout ДО старта серии:
        НЕ кладём в локальную очередь (её нет),
        а коротко ждём восстановления payout и параллельно берём самый свежий сигнал из StrategyCommon.
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
        anti_params = dict(ANTIMARTINGALE_DEFAULTS)
        if params:
            anti_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=anti_params,
            strategy_name="AntiMartingale",
            **kwargs,
        )

        self._active_series: dict[str, bool] = {}
        self._series_remaining: dict[str, int] = {}

    # =====================================================================
    # СИГНАЛЫ: ожидание / обновление контекста
    # =====================================================================

    async def _wait_for_new_signal(self, trade_key: str, *, timeout: float) -> Optional[dict]:
        """Ждём новый сигнал по trade_key из StrategyCommon."""
        common = getattr(self, "_common", None)
        if common is None:
            return None

        start = asyncio.get_event_loop().time()
        while self._running and (asyncio.get_event_loop().time() - start) < timeout:
            await self._pause_point()
            new_signal = common.pop_latest_signal(trade_key)
            if new_signal:
                return new_signal
            await asyncio.sleep(0.5)
        return None

    async def _get_next_signal_after_result(self, trade_key: str, *, timeout: float) -> Optional[dict]:
        """
        После WIN/PUSH: НЕ повторяем сделку.
        Сначала пробуем взять самый свежий сигнал из StrategyCommon, иначе ждём новый.
        Ничего не "чистим" в очереди.
        """
        common = getattr(self, "_common", None)
        if common is None:
            return None

        sig = common.pop_latest_signal(trade_key)
        if sig:
            return sig

        return await self._wait_for_new_signal(trade_key, timeout=timeout)

    def _extract_next_expire_dt(self, signal: dict) -> Optional[datetime]:
        next_expire = signal.get("next_expire")
        if isinstance(next_expire, datetime):
            if next_expire.tzinfo is None:
                return next_expire.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
            return next_expire.astimezone(ZoneInfo(MOSCOW_TZ))

        ts = signal.get("meta", {}).get("next_timestamp") if signal.get("meta") else None
        if isinstance(ts, datetime):
            return ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts.tzinfo else ts.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
        return None

    def _update_signal_context(self, new_signal: dict) -> tuple[dict, datetime, str, int, str]:
        signal_data = new_signal
        signal_received_time = new_signal["timestamp"]

        symbol = new_signal["symbol"]
        timeframe = new_signal["timeframe"]
        direction = int(new_signal["direction"])

        signal_at_str = new_signal.get("signal_time_str") or format_local_time(signal_received_time)

        self._last_signal_ver = new_signal.get("version", self._last_signal_ver)
        self._last_indicator = new_signal.get("indicator", self._last_indicator)
        self._last_signal_at_str = signal_at_str
        self._next_expire_dt = self._extract_next_expire_dt(new_signal)

        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        return signal_data, signal_received_time, timeframe, direction, signal_at_str

    # =====================================================================
    # УТИЛИТЫ
    # =====================================================================

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

    async def _is_payout_low_now(self, symbol: str) -> bool:
        min_pct = int(self.params.get("min_percent", 70))
        stake = float(self.params.get("base_investment", 100))
        account_ccy = self._anchor_ccy

        try:
            pct = await get_current_percent(
                self.http_client,
                investment=stake,
                option=symbol,
                minutes=self._trade_minutes,
                account_ccy=account_ccy,
                trade_type=self._trade_type,
            )
        except Exception:
            pct = None

        if pct is None:
            self._status("ожидание процента")
            return True

        if pct < min_pct:
            self._status("ожидание высокого процента")
            return True

        return False

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    # =====================================================================
    # ОСНОВНОЙ ВХОД
    # =====================================================================

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

        # === НИЗКИЙ PAYOUT: НЕ кладём в локальную очередь (её нет),
        # а ждём восстановления payout короткими интервалами.
        # Пока ждём — можем заменить сигнал на более свежий из StrategyCommon.
        while self._running and await self._is_payout_low_now(symbol):
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
            self._next_expire_dt = self._extract_next_expire_dt(signal_data)

            if self._use_any_symbol:
                self.symbol = symbol
            if self._use_any_timeframe:
                self.timeframe = timeframe
                self.params["timeframe"] = self.timeframe

            try:
                self._last_signal_monotonic = asyncio.get_running_loop().time()
            except RuntimeError:
                self._last_signal_monotonic = None

            # если сигнал устарел ДО старта серии — ждём новый, серию не запускаем
            while self._running:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                    mode = "classic"
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
                    mode = "sprint"

                if is_valid:
                    break

                log(signal_not_actual(symbol, mode, reason))

                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_signal = await self._wait_for_new_signal(trade_key, timeout=timeout)
                if not new_signal:
                    return

                signal_data, signal_received_time, timeframe, direction, _ = self._update_signal_context(new_signal)
                symbol = signal_data["symbol"]
                self._maybe_set_auto_minutes(timeframe)

            # серия активна
            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Антимартингейл"))

            await self._run_antimartingale_series(
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
                log(series_completed(symbol, timeframe, "Антимартингейл"))

    # =====================================================================
    # СЕРИЯ
    # =====================================================================

    async def _run_antimartingale_series(
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
        had_any_win = False

        series_direction = int(initial_direction)
        max_steps = int(self.params.get("max_steps", 3))

        signal_at_str = signal_data.get("signal_time_str") or format_local_time(signal_received_time)
        series_label = self.format_series_label(trade_key, series_left=series_left)

        base_stake = float(self.params.get("base_investment", 100))
        current_stake = base_stake

        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            # === 1) проверка актуальности перед первой сделкой в серии
            if not did_place_any_trade:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                else:
                    is_valid, reason = self._is_signal_valid_for_sprint({"timestamp": signal_received_time}, current_time)

                if not is_valid:
                    log(signal_not_actual_for_placement(symbol, reason))
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_signal = await self._wait_for_new_signal(trade_key, timeout=timeout)
                    if not new_signal:
                        return
                    signal_data, signal_received_time, timeframe, series_direction, signal_at_str = \
                        self._update_signal_context(new_signal)
                    symbol = signal_data["symbol"]
                    self._maybe_set_auto_minutes(timeframe)
                    continue

            # payout/balance (внутри серии уже умеет ждать)
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            pct, _ = await self.check_payout_and_balance(symbol, current_stake, min_pct, wait_low)
            if pct is None:
                continue

            log(
                trade_step(
                    symbol,
                    step,
                    format_amount(current_stake),
                    self._trade_minutes,
                    series_direction,
                    pct,
                )
            )

            # === 2) финальная проверка актуальности перед размещением сделки
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(
                    signal_data,
                    current_time,
                    for_placement=True,
                )
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_signal = await self._wait_for_new_signal(trade_key, timeout=timeout)
                if not new_signal:
                    return
                signal_data, signal_received_time, timeframe, series_direction, signal_at_str = \
                    self._update_signal_context(new_signal)
                symbol = signal_data["symbol"]
                self._maybe_set_auto_minutes(timeframe)
                continue

            # classic: экспирация = следующая свеча от текущего момента
            if self._trade_type == "classic":
                self._next_expire_dt = self._calc_next_candle_from_now(timeframe)

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # размещение
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, current_stake, self._anchor_ccy)
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
                current_stake,
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
                stake=float(current_stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
                step_label=step_label,
            )

            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True) + " Серия по сигналу прерывается.")
                break

            if profit > 0:
                log(win_with_parlay(symbol, format_amount(profit)))
                had_any_win = True
                current_stake += float(profit)
                step += 1

                # ✅ после WIN ждём/берём новый сигнал (очередь не чистим)
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                next_signal = await self._get_next_signal_after_result(trade_key, timeout=timeout)
                if not next_signal:
                    return

                signal_data, signal_received_time, timeframe, series_direction, signal_at_str = \
                    self._update_signal_context(next_signal)
                symbol = signal_data["symbol"]
                self._maybe_set_auto_minutes(timeframe)
                continue

            if abs(profit) < 1e-9:
                log(push_repeat_same_stake(symbol))

                # ✅ после PUSH ждём/берём новый сигнал (очередь не чистим)
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                next_signal = await self._get_next_signal_after_result(trade_key, timeout=timeout)
                if not next_signal:
                    return

                signal_data, signal_received_time, timeframe, series_direction, signal_at_str = \
                    self._update_signal_context(next_signal)
                symbol = signal_data["symbol"]
                self._maybe_set_auto_minutes(timeframe)
                continue

            log(loss_series_finish(symbol, format_amount(profit)))
            break

        # === Завершение серии (repeat_count тратим только если был WIN) ===
        if did_place_any_trade:
            if not had_any_win:
                return

            if step >= max_steps:
                log(steps_limit_reached(symbol, max_steps, flag="⛳"))

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
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
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
                    placed_at=placed_at_str,
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

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        if series_left is None:
            series_left = self._series_remaining.get(trade_key)
        return super().format_series_label(trade_key, series_left=series_left)

    def stop(self):
        super().stop()
        self._active_series.clear()
        self._series_remaining.clear()

    def update_params(self, **params):
        super().update_params(**params)
        if "repeat_count" in params:
            self._series_remaining.clear()
