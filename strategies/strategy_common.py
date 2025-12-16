from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from core.policy import (
    can_open_new_trade,
    get_current_open_trades,
    get_max_open_trades,
    release_trade_slot,
    try_acquire_trade_slot,
)
from core.time_utils import format_local_time
from strategies.constants import MOSCOW_TZ

MOSCOW_ZONE = ZoneInfo(MOSCOW_TZ)

from strategies.log_messages import (
    global_limit_before_start,
    global_lock_acquired,
    global_lock_released,
    handler_error,
    handler_stopped,
    listener_error,
    open_trades_limit,
    pending_signals_restart,
    queue_processor_started,
    queue_signal_outdated,
    removed_stale_signals,
    signal_deferred,
    signal_enqueued,
    signal_listener_started,
    signal_not_actual_generic,
    strategy_limit_deferred,
    deferred_signal_outdated,
    deferred_signal_start,
)


class StrategyCommon:
    """Общая логика для всех стратегий с системой очередей"""

    def __init__(self, strategy_instance):
        self.strategy = strategy_instance
        self.log = strategy_instance.log or (lambda s: None)
        self._single_trade_executed = False

        self._signal_queues: Dict[str, asyncio.Queue] = {}
        self._signal_processors: Dict[str, asyncio.Task] = {}
        self._pending_signals: Dict[str, asyncio.Queue] = {}
        self._pending_processing: Dict[str, asyncio.Task] = {}
        self._active_trades: Dict[str, Set[asyncio.Task]] = {}

        # Глобальная блокировка — только одна сделка в системе (если allow_parallel=False)
        self._global_trade_lock = asyncio.Lock()

    @staticmethod
    def _drain_queue(
        queue: asyncio.Queue,
        *,
        return_last: bool = False,
    ) -> int | Tuple[int, Optional[Any]]:
        removed = 0
        last_item: Optional[Any] = None
        while True:
            try:
                last_item = queue.get_nowait()
                queue.task_done()
                removed += 1
            except asyncio.QueueEmpty:
                break

        if return_last:
            return removed, last_item
        return removed

    async def signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель — кладёт в нужную очередь по trade_key"""
        log = self.log
        strategy_name = getattr(self.strategy, "strategy_name", "Strategy")
        log(signal_listener_started(strategy_name))

        while self.strategy._running:
            await self.strategy._pause_point()
            try:
                direction, ver, meta = await self.strategy._fetch_signal_payload(
                    self.strategy._last_signal_ver
                )

                meta = meta or {}
                symbol = meta.get("symbol") or self.strategy.symbol
                timeframe = (meta.get("timeframe") or self.strategy.timeframe).upper()

                # timestamp
                ts_raw = meta.get("timestamp")
                if isinstance(ts_raw, datetime):
                    signal_timestamp = (
                        ts_raw.astimezone(MOSCOW_ZONE)
                        if ts_raw.tzinfo
                        else ts_raw.replace(tzinfo=MOSCOW_ZONE)
                    )
                else:
                    signal_timestamp = datetime.now(MOSCOW_ZONE)

                # next_timestamp -> next_expire
                next_raw = meta.get("next_timestamp")
                if isinstance(next_raw, datetime):
                    next_expire = (
                        next_raw.astimezone(MOSCOW_ZONE)
                        if next_raw.tzinfo
                        else next_raw.replace(tzinfo=MOSCOW_ZONE)
                    )
                else:
                    next_expire = None

                # ПЕРВИЧНАЯ ВАЛИДАЦИЯ ДО ДОБАВЛЕНИЯ В ОЧЕРЕДЬ
                current_time = datetime.now(MOSCOW_ZONE)
                payload_for_check = {"timestamp": signal_timestamp, "next_expire": next_expire}

                if getattr(self.strategy, "_trade_type", "sprint") == "classic":
                    is_valid, reason = self.strategy._is_signal_valid_for_classic(
                        payload_for_check,
                        current_time,
                        for_placement=True,
                    )
                    mode = "classic"
                else:
                    is_valid, reason = self.strategy._is_signal_valid_for_sprint(
                        payload_for_check,
                        current_time,
                    )
                    mode = "sprint"

                if not is_valid:
                    self.strategy._last_signal_ver = ver
                    log(signal_not_actual_generic(symbol, mode, reason))
                    continue

                # direction
                try:
                    direction_value = int(direction) if direction is not None else None
                except (TypeError, ValueError):
                    direction_value = None

                signal_data = {
                    "direction": direction_value,
                    "version": int(ver),
                    "meta": meta,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": signal_timestamp,
                    "signal_time_str": format_local_time(signal_timestamp),
                    "indicator": meta.get("indicator") or "-",
                    "next_expire": next_expire,
                }

                trade_key = self.strategy.build_trade_key(symbol, timeframe)

                self.strategy._last_signal_ver = int(ver)
                self.strategy._last_signal_at_str = signal_data["signal_time_str"]

                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )

                q = self._signal_queues[trade_key]

                removed = self._drain_queue(q)
                if removed:
                    log(removed_stale_signals(symbol, removed))

                await q.put(signal_data)
                next_time_str = next_expire.strftime("%H:%M:%S") if next_expire else "?"
                log(
                    signal_enqueued(
                        symbol,
                        signal_timestamp.strftime("%H:%M:%S"),
                        next_time_str,
                    )
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(listener_error(e))
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает очередь — с глобальной блокировкой при allow_parallel=False"""
        q = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split("_", 1)
        log = self.log

        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)
        log(queue_processor_started(symbol, trade_key, allow_parallel))

        while self.strategy._running:
            await self.strategy._pause_point()
            try:
                signal_data = await q.get()

                is_valid, reason = self._validate_signal_for_processing(signal_data)
                if not is_valid:
                    symbol_to_log = signal_data.get("symbol") or symbol
                    log(queue_signal_outdated(symbol_to_log, reason))
                    q.task_done()
                    continue

                processed_immediately = False

                # ГЛОБАЛЬНЫЙ ЛИМИТ ОТКРЫТЫХ СДЕЛОК
                if not can_open_new_trade():
                    max_trades = get_max_open_trades()
                    log(global_limit_before_start(symbol, max_trades, get_current_open_trades()))
                    await self._handle_pending_signal(trade_key, signal_data)
                    processed_immediately = True

                elif not allow_parallel:
                    # Глобальная блокировка для всех символов
                    if self._single_trade_executed:
                        await self._handle_pending_signal(
                            trade_key, signal_data, schedule_processing=False
                        )
                        processed_immediately = True
                    elif self._global_trade_lock.locked():
                        await self._handle_pending_signal(trade_key, signal_data)
                        processed_immediately = True
                    else:
                        limit_reached = False
                        async with self._global_trade_lock:
                            log(global_lock_acquired(symbol))
                            acquired = await try_acquire_trade_slot()
                            if not acquired:
                                limit_reached = True
                                log(
                                    open_trades_limit(
                                        symbol,
                                        get_max_open_trades(),
                                        get_current_open_trades(),
                                        "перед стартом.",
                                    )
                                )
                            else:
                                try:
                                    task = asyncio.create_task(
                                        self.strategy._process_single_signal(signal_data)
                                    )
                                    await task
                                    self._single_trade_executed = True
                                finally:
                                    await release_trade_slot()
                            log(global_lock_released(symbol))

                        if limit_reached:
                            await self._handle_pending_signal(trade_key, signal_data)
                        processed_immediately = True

                else:
                    allow_multi = bool(
                        getattr(self.strategy, "allow_concurrent_trades_per_key", lambda: False)()
                    )
                    active_tasks = self._active_trades.get(trade_key)

                    if not allow_multi and active_tasks:
                        await self._handle_pending_signal(trade_key, signal_data)
                        processed_immediately = True
                    else:
                        acquired = await try_acquire_trade_slot()
                        if not acquired:
                            max_trades = get_max_open_trades()
                            log(open_trades_limit(symbol, max_trades, get_current_open_trades()))
                            await self._handle_pending_signal(trade_key, signal_data)
                            processed_immediately = True
                        else:
                            task = asyncio.create_task(self.strategy._process_single_signal(signal_data))
                            tasks = self._active_trades.setdefault(trade_key, set())
                            tasks.add(task)

                            def cleanup(fut: asyncio.Task, key: str = trade_key):
                                task_set = self._active_trades.get(key)
                                if task_set is not None:
                                    task_set.discard(fut)
                                    if not task_set:
                                        self._active_trades.pop(key, None)
                                q.task_done()
                                asyncio.create_task(self._check_more_pending_signals(key))
                                asyncio.create_task(release_trade_slot())

                            task.add_done_callback(cleanup)
                            continue

                if processed_immediately:
                    q.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(handler_error(symbol, e))
                q.task_done()

        log(handler_stopped(symbol, trade_key))

    def _validate_signal_for_processing(self, signal_data: dict) -> tuple[bool, str]:
        current_time = datetime.now(MOSCOW_ZONE)
        trade_type = getattr(self.strategy, "_trade_type", "sprint")

        if trade_type == "classic":
            return self.strategy._is_signal_valid_for_classic(
                signal_data,
                current_time,
                for_placement=True,
            )

        return self.strategy._is_signal_valid_for_sprint(signal_data, current_time)

    async def _handle_pending_signal(
        self, trade_key: str, signal_data: dict, *, schedule_processing: bool = True
    ):
        symbol, _ = trade_key.split("_", 1)
        log = self.log

        if trade_key not in self._pending_signals:
            self._pending_signals[trade_key] = asyncio.Queue(maxsize=1)  # только 1 слот

        self._drain_queue(self._pending_signals[trade_key])

        try:
            self._pending_signals[trade_key].put_nowait(signal_data)
        except asyncio.QueueFull:
            self._drain_queue(self._pending_signals[trade_key])
            self._pending_signals[trade_key].put_nowait(signal_data)

        log(signal_deferred(symbol))

        if schedule_processing and trade_key not in self._pending_processing:
            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

    def discard_signals_for(self, trade_key: str) -> int:
        removed = 0

        q = self._signal_queues.get(trade_key)
        if q is not None:
            removed += self._drain_queue(q)

        pending = self._pending_signals.get(trade_key)
        if pending is not None:
            removed += self._drain_queue(pending)

        return removed

    async def _process_pending_signals(self, trade_key: str):
        symbol, _ = trade_key.split("_", 1)
        log = self.log
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)

        try:
            if not allow_parallel and self._single_trade_executed:
                return

            if not allow_parallel:
                async with self._global_trade_lock:
                    log(global_lock_acquired(symbol))
                    await self._process_one_pending(trade_key)
                    log(global_lock_released(symbol))
            else:
                wait_start = asyncio.get_event_loop().time()
                while self._active_trades.get(trade_key) and self.strategy._running:
                    if asyncio.get_event_loop().time() - wait_start > 60.0:
                        break
                    await asyncio.sleep(0.1)

                if self.strategy._running:
                    wait_start = asyncio.get_event_loop().time()
                    while (
                        getattr(self.strategy, "is_series_active", lambda key: False)(trade_key)
                        and self.strategy._running
                    ):
                        if asyncio.get_event_loop().time() - wait_start > 60.0:
                            break
                        await asyncio.sleep(0.1)

                if not self.strategy._running:
                    return

                await self._process_one_pending(trade_key)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(handler_error(symbol, e))
        finally:
            self._pending_processing.pop(trade_key, None)

    async def _process_one_pending(self, trade_key: str):
        symbol, _ = trade_key.split("_", 1)
        log = self.log

        if not self.strategy.params.get("allow_parallel_trades", True) and self._single_trade_executed:
            return

        if not can_open_new_trade():
            max_trades = get_max_open_trades()
            log(
                open_trades_limit(
                    symbol,
                    max_trades,
                    get_current_open_trades(),
                    "- отложенный сигнал не обработан",
                )
            )
            self._reschedule_pending_processing(trade_key)
            return

        if trade_key not in self._pending_signals or self._pending_signals[trade_key].empty():
            return

        _, last_signal = self._drain_queue(self._pending_signals[trade_key], return_last=True)

        if not last_signal:
            return

        signal_symbol = last_signal.get("symbol") or symbol
        is_valid, reason = self._validate_signal_for_processing(last_signal)
        if not is_valid:
            log(deferred_signal_outdated(signal_symbol, reason))
            return

        log(deferred_signal_start(signal_symbol))

        acquired = await try_acquire_trade_slot()
        if not acquired:
            max_trades = get_max_open_trades()
            log(strategy_limit_deferred(signal_symbol, max_trades, get_current_open_trades()))

            q = self._pending_signals.setdefault(trade_key, asyncio.Queue(maxsize=1))
            self._drain_queue(q)
            q.put_nowait(last_signal)
            self._reschedule_pending_processing(trade_key)
            return

        if not self.strategy.params.get("allow_parallel_trades", True):
            try:
                task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
                await task
            finally:
                await release_trade_slot()
        else:
            task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
            tasks = self._active_trades.setdefault(trade_key, set())
            tasks.add(task)

            def cleanup(fut: asyncio.Task):
                task_set = self._active_trades.get(trade_key)
                if task_set is not None:
                    task_set.discard(fut)
                    if not task_set:
                        self._active_trades.pop(trade_key, None)
                asyncio.create_task(release_trade_slot())

            task.add_done_callback(cleanup)

    async def _check_more_pending_signals(self, trade_key: str):
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            symbol, _ = trade_key.split("_", 1)
            log = self.log
            log(pending_signals_restart(symbol))

            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )

    def _reschedule_pending_processing(self, trade_key: str, delay: float = 0.5) -> None:
        async def _schedule():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            if not self.strategy._running:
                return

            q = self._pending_signals.get(trade_key)
            if q is None or q.empty():
                return

            if trade_key in self._pending_processing:
                return

            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

        asyncio.create_task(_schedule())

    def pop_latest_signal(self, trade_key: str) -> Optional[dict]:
        q = self._pending_signals.get(trade_key)
        if q is None or q.empty():
            return None

        _, latest_signal = self._drain_queue(q, return_last=True)
        return latest_signal

    def stop(self):
        all_tasks: list[asyncio.Task] = []
        all_tasks.extend(self._signal_processors.values())
        all_tasks.extend(self._pending_processing.values())
        for task_set in self._active_trades.values():
            all_tasks.extend(task_set)

        for task in all_tasks:
            if not task.done():
                task.cancel()

        for q in list(self._signal_queues.values()):
            self._drain_queue(q)

        for q in list(self._pending_signals.values()):
            self._drain_queue(q)

        self._signal_queues.clear()
        self._signal_processors.clear()
        self._pending_signals.clear()
        self._pending_processing.clear()
        self._active_trades.clear()
