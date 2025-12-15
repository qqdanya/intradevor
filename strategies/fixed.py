# strategies/fixed_stake_strategy.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

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
    start_processing,
    signal_not_actual,
    signal_not_actual_for_placement,
    trade_placement_failed,
    trade_summary,
    result_unknown,
    result_win,
    result_loss,
    trade_limit_reached,
    fixed_stake_stopped,
    trade_timeout,
)

FIXED_DEFAULTS = {
    "base_investment": 100,
    "repeat_count": 10,  # лимит сделок
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": None,  # ждём всю длительность сделки
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class FixedStakeStrategy(BaseTradingStrategy):
    """
    Fixed Stake под новую архитектуру StrategyCommon:

    - 1 сделка на сигнал
    - лимит repeat_count:
        * allow_parallel_trades=True  -> по trade_key (используем BaseTradingStrategy series_counters)
        * allow_parallel_trades=False -> глобальный (один общий счётчик на стратегию)
    - low payout: коротко ждём, при этом подхватываем свежий pending сигнал
    - если сигнал устарел перед размещением: ждём новый сигнал, лимит НЕ уменьшаем
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

        # глобальный лимит при allow_parallel_trades=False
        self._global_left: Optional[int] = None
        self._global_lock: asyncio.Lock = asyncio.Lock()

        # блокировка trade_key при запрете параллели (на уровне стратегии)
        self._active_trade: dict[str, bool] = {}

        # статистика
        self._placed_trades_total: int = 0

        # per-key locks, чтобы атомарно списывать лимит в parallel режиме
        self._series_locks: dict[str, asyncio.Lock] = {}

    # =====================================================================
    # helpers
    # =====================================================================

    def _get_series_lock(self, trade_key: str) -> asyncio.Lock:
        lock = self._series_locks.get(trade_key)
        if lock is None:
            lock = asyncio.Lock()
            self._series_locks[trade_key] = lock
        return lock

    def allow_concurrent_trades_per_key(self) -> bool:
        return bool(self.params.get("allow_parallel_trades", True))

    def is_series_active(self, trade_key: str) -> bool:
        # Для FixedStake серии нет, но если параллель запрещена — блокируем ключ
        if self.allow_concurrent_trades_per_key():
            return False
        return self._active_trade.get(trade_key, False)

    def _ensure_global_left(self) -> int:
        if self._global_left is None:
            try:
                total = int(self.params.get("repeat_count", 0))
            except Exception:
                total = 0
            self._global_left = max(0, total)
        return int(self._global_left)

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        # для FixedStake серия всегда 1/1
        return "1/1"

    def _format_step_label(self, total: int, current_left: int) -> Optional[str]:
        if total <= 0:
            return None
        # current_left -> сколько осталось ДО списания
        current = max(1, min(total, total - int(current_left) + 1))
        return f"{current}/{total}"

    # =====================================================================
    # main
    # =====================================================================

    async def _process_single_signal(self, signal_data: dict) -> None:
        log = self.log or (lambda s: None)

        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = int(signal_data["direction"])

        self._maybe_set_auto_minutes(timeframe)
        trade_key = self.build_trade_key(symbol, timeframe)

        # Если параллель запрещена — паркуем сигнал (как в Fibonacci)
        if not self.allow_concurrent_trades_per_key() and self._active_trade.get(trade_key):
            common = getattr(self, "_common", None)
            if common is not None:
                await common._handle_pending_signal(trade_key, signal_data)
            return

        # --- Лимит сделок ---
        if self.allow_concurrent_trades_per_key():
            series_left = self._get_series_left(trade_key)
            if series_left <= 0:
                if not self._stop_when_idle_requested:
                    done = int(self.params.get("repeat_count", 0))
                    log(trade_limit_reached(symbol, done, done))
                    self._status("достигнут лимит сделок")
                self._request_stop_when_idle("достигнут лимит сделок")
                return
        else:
            async with self._global_lock:
                left = self._ensure_global_left()
                if left <= 0:
                    if not self._stop_when_idle_requested:
                        done = int(self.params.get("repeat_count", 0))
                        log(trade_limit_reached(symbol, done, done))
                        self._status("достигнут лимит сделок")
                    self._request_stop_when_idle("достигнут лимит сделок")
                    return

        # обновление контекста сигнала
        signal_data, signal_received_time, direction, signal_at_str = update_signal_context(
            self,
            signal_data,
            update_symbol=self._use_any_symbol,
            update_timeframe=self._use_any_timeframe,
        )
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        trade_key = self.build_trade_key(symbol, timeframe)

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        log(start_processing(symbol, "Fixed Stake"))

        # базовая валидация сигнала (до ожиданий)
        now = self.now_moscow()
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
            if not ok:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            ok, reason = self._is_signal_valid_for_sprint(signal_data, now)
            if not ok:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        # LOW PAYOUT: коротко ждём и подхватываем свежий pending сигнал
        stake = float(self.params.get("base_investment", 100))
        wait_low = float(self.params.get("wait_on_low_percent", 1))

        while self._running and await is_payout_low_now(self, symbol):
            await self._pause_point()

            common = getattr(self, "_common", None)
            if common is not None:
                newer = common.pop_latest_signal(trade_key)
                if newer:
                    signal_data, signal_received_time, direction, signal_at_str = update_signal_context(
                        self,
                        newer,
                        update_symbol=self._use_any_symbol,
                        update_timeframe=self._use_any_timeframe,
                    )
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    self._maybe_set_auto_minutes(timeframe)
                    trade_key = self.build_trade_key(symbol, timeframe)

            await asyncio.sleep(wait_low)

        # финальная проверка перед размещением: если устарел — ждём новый сигнал, лимит НЕ уменьшаем
        now = self.now_moscow()
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, now, for_placement=True)
        else:
            payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
            ok, reason = self._is_signal_valid_for_sprint(payload, now)

        if not ok:
            log(signal_not_actual_for_placement(symbol, reason))
            timeout = float(self.params.get("signal_timeout_sec", 30.0))
            new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
            if not new_signal:
                log(trade_timeout(symbol, timeout))
                return
            # просто повторно обработаем новый сигнал (лимит пока не трогали)
            await self._process_single_signal(new_signal)
            return

        # classic: экспирация = следующая свеча от "сейчас"
        if self._trade_type == "classic":
            self._next_expire_dt = calc_next_candle_from_now(timeframe)

        # summary (перед размещением)
        min_pct = int(self.params.get("min_percent", 70))
        pct_now, _bal = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
        if pct_now is None:
            return

        log(
            trade_summary(
                symbol,
                format_amount(stake),
                self._trade_minutes,
                direction,
                int(pct_now),
            )
        )

        # блокировка ключа при запрете параллели
        locked = False
        if not self.allow_concurrent_trades_per_key():
            self._active_trade[trade_key] = True
            locked = True

        try:
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")

            total = int(self.params.get("repeat_count", 0)) or 0
            series_label = self.format_series_label(trade_key)

            step_label: Optional[str] = None
            left_after_place: Optional[int] = None

            # ---------------- размещение + списание лимита атомарно ----------------
            if self.allow_concurrent_trades_per_key():
                async with self._get_series_lock(trade_key):
                    current_left = self._get_series_left(trade_key)
                    if current_left <= 0:
                        if not self._stop_when_idle_requested:
                            done = int(self.params.get("repeat_count", 0))
                            log(trade_limit_reached(symbol, done, done))
                            self._status("достигнут лимит сделок")
                        self._request_stop_when_idle("достигнут лимит сделок")
                        return

                    trade_id = await self.place_trade_with_retry(symbol, direction, stake, self._anchor_ccy)
                    if not trade_id:
                        log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                        return  # лимит НЕ уменьшаем

                    new_left = max(0, int(current_left) - 1)
                    self._set_series_left(trade_key, new_left)
                    left_after_place = new_left
                    step_label = self._format_step_label(total, current_left)

            else:
                async with self._global_lock:
                    current_left = self._ensure_global_left()
                    if current_left <= 0:
                        if not self._stop_when_idle_requested:
                            done = int(self.params.get("repeat_count", 0))
                            log(trade_limit_reached(symbol, done, done))
                            self._status("достигнут лимит сделок")
                        self._request_stop_when_idle("достигнут лимит сделок")
                        return

                    trade_id = await self.place_trade_with_retry(symbol, direction, stake, self._anchor_ccy)
                    if not trade_id:
                        log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                        return  # лимит НЕ уменьшаем

                    new_left = max(0, int(current_left) - 1)
                    self._global_left = new_left
                    left_after_place = new_left
                    step_label = self._format_step_label(total, current_left)

            if "trade_id" not in locals() or not trade_id:
                return

            self._placed_trades_total += 1

            trade_seconds, expected_end_ts = self.trade_duration()
            wait_cfg = self.params.get("result_wait_s")
            wait_seconds = trade_seconds if wait_cfg is None or float(wait_cfg) <= 0 else float(wait_cfg)

            # pending notify
            self._register_pending_trade(trade_id, symbol, timeframe)

            self.notify_pending_trade(
                trade_id=str(trade_id),
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=float(stake),
                percent=int(pct_now),
                trade_seconds=float(trade_seconds),
                account_mode=account_mode,
                expected_end_ts=float(expected_end_ts),
                signal_at=signal_at_str,
                series_label=series_label,
                step_label=step_label,
            )

            # результат
            self._spawn_result_checker(
                trade_id=str(trade_id),
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=float(stake),
                percent=int(pct_now),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
                step_label=step_label,
            )

            if (left_after_place or 0) > 0:
                self._status(f"сделок осталось: {left_after_place}")
            else:
                self._status("достигнут лимит сделок")
                self._request_stop_when_idle("достигнут лимит сделок")

        finally:
            if locked:
                self._active_trade.pop(trade_key, None)

    # =====================================================================
    # stop / params
    # =====================================================================

    def stop(self) -> None:
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._placed_trades_total))
        super().stop()
        self._active_trade.clear()
        self._series_locks.clear()

    def update_params(self, **params) -> None:
        super().update_params(**params)

        # при смене repeat_count и allow_parallel_trades=False — переинициализируем глобальный счётчик
        if "repeat_count" in params and not self.allow_concurrent_trades_per_key():
            try:
                total = int(self.params.get("repeat_count", 0))
            except Exception:
                total = 0
            self._global_left = max(0, total)
