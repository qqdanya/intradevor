# strategies/oscar_grind_strategy.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Dict, Any

from strategies.base_trading_strategy import BaseTradingStrategy
from strategies.strategy_helpers import (
    calc_next_candle_from_now,
    is_payout_low_now,
    update_signal_context,
    wait_for_new_signal,
)
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    trade_summary,
    series_completed,
    target_profit_reached,
    series_remaining_oscar,
    steps_limit_reached,
    trade_timeout,
)

OSCAR_GRIND_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 20,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 300,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "double_entry": True,   # повтор одной и той же ставки после LOSS без нового сигнала
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class OscarGrindStrategy(BaseTradingStrategy):
    """
    Oscar Grind под новую унифицированную архитектуру:

    - Цель серии: заработать target_profit = base_unit (по умолчанию)
    - Ставка выбирается так, чтобы при WIN добрать "need" до target_profit (округление вверх)
    - Серия живёт по trade_key (пара/ТФ)
    - LOW payout до старта: коротко ждём, подхватывая свежий pending сигнал
    - Если сигнал устарел перед размещением: ждём новый сигнал, серию НЕ завершаем
    - double_entry: один повтор после LOSS без проверки сигнала (как было)
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
        merged = dict(OSCAR_GRIND_DEFAULTS)
        if params:
            merged.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=merged,
            strategy_name="OscarGrind",
            **kwargs,
        )

        self._active_series: Dict[str, bool] = {}
        self._series_state: Dict[str, dict] = {}

    # =====================================================================
    # StrategyCommon integration
    # =====================================================================

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    # =====================================================================
    # Entry point
    # =====================================================================

    async def _process_single_signal(self, signal_data: dict) -> None:
        log = self.log or (lambda s: None)

        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = int(signal_data["direction"])

        self._maybe_set_auto_minutes(timeframe)
        trade_key = self.build_trade_key(symbol, timeframe)

        # если серия активна -> pending
        if self._active_series.get(trade_key):
            log(series_already_active(symbol, timeframe))
            common = getattr(self, "_common", None)
            if common is not None:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        # low payout ДО старта серии
        while self._running and await is_payout_low_now(self, symbol):
            await self._pause_point()

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

            await asyncio.sleep(float(self.params.get("wait_on_low_percent", 1)))

        log(start_processing(symbol, "Oscar Grind"))

        # обновляем контекст
        self._last_signal_ver = signal_data.get("version", self._last_signal_ver)
        self._last_indicator = signal_data.get("indicator", self._last_indicator)
        signal_received_time = signal_data["timestamp"]
        self._last_signal_at_str = signal_data.get("signal_time_str") or self._last_signal_at_str
        self._next_expire_dt = signal_data.get("next_expire")

        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # Важно: если сигнал неактуален — ждём новый, а не выходим
        while self._running:
            now = self.now_moscow()
            if self._trade_type == "classic":
                ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            else:
                ok, reason = self._is_signal_valid_for_sprint(signal_data, now)

            if ok:
                break

            log(signal_not_actual_for_placement(symbol, reason))
            timeout = float(self.params.get("signal_timeout_sec", 30.0))
            new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
            if not new_sig:
                log(trade_timeout(symbol, timeout))
                return

            signal_data, signal_received_time, direction, _ = update_signal_context(self, new_sig)
            symbol = signal_data["symbol"]
            timeframe = signal_data["timeframe"]
            self._maybe_set_auto_minutes(timeframe)
            trade_key = self.build_trade_key(symbol, timeframe)

        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        started = False
        try:
            self._active_series[trade_key] = True
            started = True

            updated = await self._run_oscar_series(
                trade_key=trade_key,
                symbol=symbol,
                timeframe=timeframe,
                initial_direction=int(direction),
                series_left=series_left,
                signal_received_time=signal_received_time,
                signal_data=signal_data,
                log=log,
            )
            self._set_series_left(trade_key, updated)

        finally:
            if started:
                self._active_series.pop(trade_key, None)
                log(series_completed(symbol, timeframe, "Oscar Grind"))

    # =====================================================================
    # Series
    # =====================================================================

    async def _run_oscar_series(
        self,
        *,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        series_left: int,
        signal_received_time: datetime,
        signal_data: dict,
        log,
    ) -> int:
        base_unit = float(self.params.get("base_investment", 100))
        target_profit = base_unit
        max_steps = int(self.params.get("max_steps", 20))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        double_entry = bool(self.params.get("double_entry", True))

        if max_steps <= 0:
            return series_left

        # восстановление state (если серия была прервана остановкой/перезапуском логики)
        state = self._series_state.get(trade_key) or {}
        step_idx = int(state.get("step_idx", 0))
        cum_profit = float(state.get("cum_profit", 0.0))
        stake = float(state.get("stake", base_unit))
        series_started = bool(state.get("series_started", False))

        needs_signal_validation = True
        series_direction = int(initial_direction)
        has_repeated = bool(state.get("has_repeated", False))
        skip_signal_checks_once = False
        require_new_signal = False

        signal_at_str = signal_data.get("signal_time_str") or self._last_signal_at_str
        series_label = self.format_series_label(trade_key, series_left=series_left)

        def _refresh_from(new_sig: Optional[dict]) -> None:
            nonlocal signal_data, signal_received_time, series_direction, signal_at_str, needs_signal_validation
            if not new_sig:
                return
            signal_data, signal_received_time, series_direction, signal_at_str = update_signal_context(self, new_sig)
            needs_signal_validation = True

        while self._running and step_idx < max_steps:
            await self._pause_point()

            if not await self.ensure_account_conditions():
                continue

            if require_new_signal and not skip_signal_checks_once:
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_sig:
                    log(trade_timeout(symbol, timeout))
                    break
                _refresh_from(new_sig)
                symbol = signal_data["symbol"]
                timeframe = signal_data["timeframe"]
                self._maybe_set_auto_minutes(timeframe)
                require_new_signal = False
                has_repeated = False

            skip_checks = skip_signal_checks_once
            if skip_signal_checks_once:
                skip_signal_checks_once = False

            # 1) предварительная проверка актуальности (если нужно)
            if needs_signal_validation and not skip_checks:
                now = self.now_moscow()
                if self._trade_type == "classic":
                    ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
                else:
                    payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
                    ok, reason = self._is_signal_valid_for_sprint(payload, now)

                if not ok:
                    log(signal_not_actual_for_placement(symbol, reason))
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                    if not new_sig:
                        log(trade_timeout(symbol, timeout))
                        return series_left
                    _refresh_from(new_sig)
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    self._maybe_set_auto_minutes(timeframe)
                    has_repeated = False
                    continue

            # 2) payout/balance
            pct, _ = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(
                trade_summary(symbol, format_amount(stake), self._trade_minutes, series_direction, pct)
                + f" (step {step_idx + 1})"
            )

            # 3) финальная проверка перед размещением
            if not skip_checks:
                now = self.now_moscow()
                if self._trade_type == "classic":
                    ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
                else:
                    payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
                    ok, reason = self._is_signal_valid_for_sprint(payload, now)

                if not ok:
                    log(signal_not_actual_for_placement(symbol, reason))
                    timeout = float(self.params.get("signal_timeout_sec", 30.0))
                    new_sig = await wait_for_new_signal(self, trade_key, timeout=timeout)
                    if not new_sig:
                        log(trade_timeout(symbol, timeout))
                        return series_left
                    _refresh_from(new_sig)
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    self._maybe_set_auto_minutes(timeframe)
                    continue

            needs_signal_validation = False

            # classic: экспирация = следующая свеча от сейчас
            if self._trade_type == "classic":
                self._next_expire_dt = calc_next_candle_from_now(timeframe)

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # 4) размещение
            self._status("ставка")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Ждем новый сигнал."))
                await self.sleep(2.0)
                require_new_signal = True
                needs_signal_validation = True
                continue

            series_started = True

            trade_seconds, expected_end_ts = self.trade_duration()
            wait_cfg = self.params.get("result_wait_s")
            wait_seconds = float(trade_seconds if wait_cfg is None else wait_cfg)

            step_label = self.format_step_label(step_idx, max_steps)

            self._register_pending_trade(trade_id, symbol, timeframe)

            self.notify_pending_trade(
                trade_id=str(trade_id),
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                trade_seconds=float(trade_seconds),
                account_mode=account_mode,
                expected_end_ts=float(expected_end_ts),
                signal_at=signal_at_str,
                series_label=series_label,
                step_label=step_label,
            )
            self._spawn_result_checker(
                trade_id=str(trade_id),
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
            break

        # дошли до лимита шагов
        if step_idx >= max_steps:
            log(steps_limit_reached(symbol, max_steps))

        # серия считается завершенной, если была начата (есть хотя бы одна сделка)
        if series_started:
            series_left = max(0, int(series_left) - 1)
            log(series_remaining_oscar(symbol, series_left))
            self._series_state.pop(trade_key, None)

        return series_left

    # =====================================================================
    # Stake rule (core of Oscar Grind)
    # =====================================================================

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
    ) -> float:
        """
        Простой Oscar Grind:

        - Если win/refund: ставим base_unit
        - Если loss: ставим base_unit, либо (если need > expected_win(base_unit)) увеличиваем,
          чтобы при следующем win покрыть need.
        """
        p = max(1.0, float(pct)) / 100.0

        # ожидаемый профит = stake * p
        def expected_profit(x: float) -> float:
            return float(x) * p

        # базовая ставка
        s = float(base_unit)

        # если нужно добрать больше, чем даст base_unit при win — увеличиваем
        if need > expected_profit(s):
            # хотим expected_profit(stake) >= need  -> stake >= need/p
            s = max(s, (need / p) if p > 0 else s)

        # защита от нулей
        return max(1.0, float(s))

    # =====================================================================
    # Stop
    # =====================================================================

    def stop(self) -> None:
        super().stop()
        self._series_state.clear()
        self._active_series.clear()
