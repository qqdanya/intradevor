from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from strategies.base_trading_strategy import BaseTradingStrategy
from strategies.strategy_helpers import (
    MOSCOW_ZONE,
    calc_next_candle_from_now,
    is_payout_low_now,
    update_signal_context,
    wait_for_new_signal,
)
from core.money import format_amount
from core.intrade_api_async import (
    is_demo_account,
)
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
    trade_timeout,  # используется в _wait_for_new_signal (как в Fibonacci)
)

FIXED_DEFAULTS = {
    "base_investment": 100,
    "repeat_count": 10,  # лимит сделок
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    # Для фиксированной ставки ждём результат всю длительность сделки (classic/sprint)
    "result_wait_s": None,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class FixedStakeStrategy(BaseTradingStrategy):
    """
    Fixed Stake (1 сделка на сигнал), адаптировано под новую архитектуру:
    - при allow_parallel_trades=True:
        repeat_count/step считаем по trade_key (как было), списание атомарно под Lock (из-за гонок)
    - при allow_parallel_trades=False:
        ОДНА глобальная серия шагов на всю стратегию (не зависит от symbol/timeframe),
        step инкрементится при каждой успешно размещённой сделке.
    - при LOW payout — коротко ждём и подхватываем самый свежий сигнал из StrategyCommon
    - при устаревании перед размещением — ждём новый сигнал, лимит НЕ "съедаем"
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

        # Используем как "флаг занятости" только если allow_parallel_trades=False
        self._active_trade: dict[str, bool] = {}

        # Счётчик успешно размещённых сделок (общий, для stop-лога)
        self._placed_trades_total: int = 0

        # Locks для атомарного списания repeat_count по trade_key (важно при allow_parallel_trades=True)
        self._series_locks: dict[str, asyncio.Lock] = {}

        # Глобальная серия для allow_parallel_trades=False
        self._global_left: Optional[int] = None  # инициализируем лениво из repeat_count
        self._global_lock: asyncio.Lock = asyncio.Lock()

    # =====================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # =====================================================================

    def _get_series_lock(self, trade_key: str) -> asyncio.Lock:
        lock = self._series_locks.get(trade_key)
        if lock is None:
            lock = asyncio.Lock()
            self._series_locks[trade_key] = lock
        return lock

    def allow_concurrent_trades_per_key(self) -> bool:
        """Можно ли параллелить (если allow_parallel_trades=True)."""
        return bool(self.params.get("allow_parallel_trades", True))

    def is_series_active(self, trade_key: str) -> bool:
        """Для FixedStake серия как таковая отсутствует, но при запрете параллели блокируем trade_key."""
        if self.allow_concurrent_trades_per_key():
            return False
        return self._active_trade.get(trade_key, False)

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки (classic по next_expire_dt, иначе minutes)."""
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - datetime.now(MOSCOW_ZONE)).total_seconds(),
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds

        return trade_seconds, expected_end_ts

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        return "1/1"

    def format_step_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        """
        step для UI:
        - allow_parallel_trades=False -> глобальный шаг
        - allow_parallel_trades=True  -> шаг по trade_key (как раньше)
        """
        try:
            total = int(self.params.get("repeat_count", 0))
        except Exception:
            total = 0
        if total <= 0:
            return None

        # Глобально показывать можно только если уже инициализировано (иначе будет "1/10" до первой сделки)
        if not self.allow_concurrent_trades_per_key():
            if self._global_left is None:
                # ещё ничего не размещали
                current = 1
            else:
                current = max(1, min(total, total - int(self._global_left) + 1))
            return f"{current}/{total}"

        # По trade_key
        if series_left is None:
            series_left = self._get_series_left(trade_key)

        try:
            remaining = int(series_left)
        except Exception:
            remaining = total

        current = max(1, min(total, total - remaining + 1))
        return f"{current}/{total}"

    def _ensure_global_left(self) -> int:
        if self._global_left is None:
            try:
                total = int(self.params.get("repeat_count", 0))
            except Exception:
                total = 0
            self._global_left = max(0, total)
        return int(self._global_left)

    # =====================================================================
    # ОСНОВНАЯ ЛОГИКА
    # =====================================================================

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Fixed Stake (1 сделка)."""
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = int(signal_data["direction"])

        self._maybe_set_auto_minutes(timeframe)
        trade_key = self.build_trade_key(symbol, timeframe)

        log = self.log or (lambda s: None)

        # Если параллель запрещена — паркуем сигнал в StrategyCommon, как в Fibonacci
        if not self.allow_concurrent_trades_per_key() and self._active_trade.get(trade_key):
            if hasattr(self, "_common") and self._common is not None:
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        # Лимит сделок:
        # - allow_parallel=True  -> по trade_key (series_left)
        # - allow_parallel=False -> глобально (self._global_left)
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

        signal_data, signal_received_time, direction, signal_at_str = update_signal_context(
            self,
            signal_data,
            update_symbol=self._use_any_symbol,
            update_timeframe=self._use_any_timeframe,
        )
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        log(start_processing(symbol, "Fixed Stake"))

        # Валидация сигнала (до любых ожиданий)
        current_time = datetime.now(MOSCOW_ZONE)
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not ok:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            ok, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not ok:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        # LOW PAYOUT: коротко ждём и подхватываем самый свежий сигнал
        stake = float(self.params.get("base_investment", 100))
        wait_low = float(self.params.get("wait_on_low_percent", 1))

        while self._running:
            await self._pause_point()
            if not await is_payout_low_now(self, symbol):
                break

            # пока ждём — берём свежий сигнал (если есть)
            if hasattr(self, "_common") and self._common is not None:
                newer = self._common.pop_latest_signal(trade_key)
                if newer:
                    (
                        signal_data,
                        signal_received_time,
                        direction,
                        signal_at_str,
                    ) = update_signal_context(
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

        # Финальная проверка актуальности перед размещением:
        # если устарел — ждём новый сигнал, лимит НЕ уменьшаем
        current_time = datetime.now(MOSCOW_ZONE)
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
        else:
            sprint_payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_received_time}
            ok, reason = self._is_signal_valid_for_sprint(sprint_payload, current_time)

        if not ok:
            log(signal_not_actual_for_placement(symbol, reason))
            if hasattr(self, "_common") and self._common is not None:
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_signal = await wait_for_new_signal(self, trade_key, timeout=timeout)
                if not new_signal:
                    log(trade_timeout(symbol, timeout))
                    return
                await self._process_single_signal(new_signal)
            return

        # classic: экспирация = следующая свеча от "сейчас"
        if self._trade_type == "classic":
            self._next_expire_dt = calc_next_candle_from_now(timeframe)

        # summary
        min_pct = int(self.params.get("min_percent", 70))
        pct_now, _balance = await self.check_payout_and_balance(
            symbol, stake, min_pct, wait_low
        )
        if pct_now is None:
            return
        pct_now = int(pct_now)

        log(
            trade_summary(
                symbol,
                format_amount(stake),
                self._trade_minutes,
                direction,
                pct_now,
            )
        )

        # lock trade_key если параллель запрещена
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
            series_left_after_place: Optional[int] = None  # для статуса

            # -----------------------------------------------------------------
            # Размещение + списание лимита (атомарно)
            # -----------------------------------------------------------------
            if self.allow_concurrent_trades_per_key():
                # allow_parallel=True -> по trade_key
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
                    series_left_after_place = new_left

                    # шаг по trade_key
                    if total > 0:
                        current_step = max(1, min(total, total - int(current_left) + 1))
                        step_label = f"{current_step}/{total}"

            else:
                # allow_parallel=False -> глобально (не зависит от пары/ТФ)
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
                    series_left_after_place = new_left

                    # шаг глобальный
                    if total > 0:
                        current_step = max(1, min(total, total - int(current_left) + 1))
                        step_label = f"{current_step}/{total}"

            # если trade_id не объявлен — значит не разместили
            if "trade_id" not in locals() or not trade_id:
                return

            self._placed_trades_total += 1

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds_cfg = self.params.get("result_wait_s")
            wait_seconds = (
                trade_seconds
                if wait_seconds_cfg is None or float(wait_seconds_cfg) <= 0
                else float(wait_seconds_cfg)
            )

            signal_at_str = signal_data.get("signal_time_str") or self._last_signal_at_str

            # pending notify
            self._set_planned_stake(trade_key, stake)
            if callable(self._on_trade_pending):
                try:
                    self._on_trade_pending(
                        trade_id=trade_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        signal_at=signal_at_str,
                        placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                        direction=direction,
                        stake=float(stake),
                        percent=int(pct_now),
                        wait_seconds=float(trade_seconds),
                        account_mode=account_mode,
                        indicator=self._last_indicator,
                        expected_end_ts=expected_end_ts,
                        series=series_label,
                        step=step_label,
                    )
                except Exception:
                    pass

            self._register_pending_trade(trade_id, symbol, timeframe)

            # Ждём результат
            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
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

            if profit is None:
                log(result_unknown(symbol))
            elif profit >= 0:
                log(result_win(symbol, f"Результат: {format_amount(profit)}"))
            else:
                log(result_loss(symbol, f"Результат: {format_amount(profit)}"))

            if (series_left_after_place or 0) > 0:
                self._status(f"сделок осталось: {series_left_after_place}")
            else:
                self._status("достигнут лимит сделок")
                self._request_stop_when_idle("достигнут лимит сделок")

        finally:
            if locked:
                self._active_trade.pop(trade_key, None)

    def stop(self):
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._placed_trades_total))
        super().stop()

    def update_params(self, **params):
        super().update_params(**params)

        # Если меняем repeat_count на лету — глобальный остаток логично переинициализировать,
        # но чтобы не ломать текущую серию, делаем это только если пользователь явно передал repeat_count.
        if "repeat_count" in params and not self.allow_concurrent_trades_per_key():
            try:
                total = int(self.params.get("repeat_count", 0))
            except Exception:
                total = 0
            # начинаем новую "глобальную" серию
            self._global_left = max(0, total)
