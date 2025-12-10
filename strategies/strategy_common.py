from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Set
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
        
        # Очереди и задачи для параллельной обработки
        self._signal_queues: Dict[str, asyncio.Queue] = {}
        self._signal_processors: Dict[str, asyncio.Task] = {}
        self._pending_signals: Dict[str, asyncio.Queue] = {}
        self._pending_processing: Dict[str, asyncio.Task] = {}
        self._active_trades: Dict[str, Set[asyncio.Task]] = {}
        
        # Глобальная блокировка — только одна сделка в системе
        self._global_trade_lock = asyncio.Lock()

    async def signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель — кладёт в нужную очередь по trade_key"""
        log = self.log
        strategy_name = getattr(self.strategy, 'strategy_name', 'Strategy')
        log(signal_listener_started(strategy_name))
        
        while self.strategy._running:
            await self.strategy._pause_point()
            try:
                direction, ver, meta = await self.strategy._fetch_signal_payload(self.strategy._last_signal_ver)
                
                # Извлекаем timestamp и next_timestamp
                signal_timestamp = datetime.now(ZoneInfo(MOSCOW_TZ))
                next_expire = None
                if meta and isinstance(meta, dict):
                    ts_raw = meta.get('timestamp')
                    if ts_raw and isinstance(ts_raw, datetime):
                        signal_timestamp = ts_raw.astimezone(ZoneInfo(MOSCOW_TZ))
                    
                    next_raw = meta.get('next_timestamp')
                    if next_raw and isinstance(next_raw, datetime):
                        next_expire = next_raw.astimezone(ZoneInfo(MOSCOW_TZ))
                
                # ПРОВЕРКА АКТУАЛЬНОСТИ ПЕРЕД ДОБАВЛЕНИЕМ В ОЧЕРЕДЬ
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                symbol = meta.get('symbol') if meta else self.strategy.symbol
                
                if self.strategy._trade_type == "classic":
                    # Используем новую логику проверки для classic
                    is_valid, reason = self.strategy._is_signal_valid_for_classic(
                        {
                            'timestamp': signal_timestamp,
                            'next_expire': next_expire
                        },
                        current_time
                    )
                    if not is_valid:
                        # Обновляем версию, чтобы не обрабатывать сигнал повторно
                        self.strategy._last_signal_ver = ver
                        log(signal_not_actual_generic(symbol, "classic", reason))
                        continue
                else:
                    # Для sprint - используем старую логику
                    is_valid, reason = self.strategy._is_signal_valid_for_sprint(
                        {
                            'timestamp': signal_timestamp
                        },
                        current_time
                    )
                    if not is_valid:
                        # Обновляем версию, чтобы не обрабатывать сигнал повторно
                        self.strategy._last_signal_ver = ver
                        log(signal_not_actual_generic(symbol, "sprint", reason))
                        continue

                direction_value: Optional[int]
                try:
                    direction_value = int(direction) if direction is not None else None
                except (TypeError, ValueError):
                    direction_value = None

                signal_data = {
                    'direction': direction_value,
                    'version': ver,
                    'meta': meta,
                    'symbol': meta.get('symbol') if meta else self.strategy.symbol,
                    'timeframe': meta.get('timeframe') if meta else self.strategy.timeframe,
                    'timestamp': signal_timestamp,
                    'signal_time_str': format_local_time(signal_timestamp),
                    'indicator': meta.get('indicator') if meta else '-',
                    'next_expire': next_expire,
                }

                symbol = signal_data['symbol']
                timeframe = signal_data['timeframe']
                trade_key = self.strategy.build_trade_key(symbol, timeframe)

                self.strategy._last_signal_ver = ver
                self.strategy._last_signal_at_str = signal_data['signal_time_str']
                
                # Создаём очередь если её нет
                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )

                queue = self._signal_queues[trade_key]

                # Удаляем предыдущие сигналы для этой пары и таймфрейма,
                # чтобы в очереди всегда оставался только самый свежий
                removed = 0
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                        removed += 1
                    except asyncio.QueueEmpty:
                        break

                if removed:
                    log(removed_stale_signals(symbol, removed))

                await queue.put(signal_data)
                next_time_str = next_expire.strftime('%H:%M:%S') if next_expire else '?'
                log(
                    signal_enqueued(
                        symbol, signal_timestamp.strftime('%H:%M:%S'), next_time_str
                    )
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(listener_error(e))
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает очередь — с глобальной блокировкой при allow_parallel=False"""
        queue = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log
        
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)
        log(queue_processor_started(symbol, trade_key, allow_parallel))
        
        while self.strategy._running:
            await self.strategy._pause_point()
            try:
                signal_data = await queue.get()

                is_valid, reason = self._validate_signal_for_processing(signal_data)
                if not is_valid:
                    symbol_to_log = signal_data.get('symbol') or symbol
                    log(queue_signal_outdated(symbol_to_log, reason))
                    queue.task_done()
                    continue

                processed_immediately = False

                # ПРОВЕРКА ЛИМИТА ОТКРЫТЫХ СДЕЛОК
                if not can_open_new_trade():
                    max_trades = get_max_open_trades()
                    log(
                        global_limit_before_start(
                            symbol, max_trades, get_current_open_trades()
                        )
                    )
                    await self._handle_pending_signal(trade_key, signal_data)
                    processed_immediately = True

                elif not allow_parallel:
                    # Глобальная блокировка для всех символов
                    if self._global_trade_lock.locked():
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
                            task = asyncio.create_task(
                                self.strategy._process_single_signal(signal_data)
                            )
                            tasks = self._active_trades.setdefault(trade_key, set())
                            tasks.add(task)

                            def cleanup(fut: asyncio.Task, key: str = trade_key):
                                task_set = self._active_trades.get(key)
                                if task_set is not None:
                                    task_set.discard(fut)
                                    if not task_set:
                                        self._active_trades.pop(key, None)
                                queue.task_done()
                                asyncio.create_task(self._check_more_pending_signals(key))
                                asyncio.create_task(release_trade_slot())

                            task.add_done_callback(cleanup)
                            continue

                if processed_immediately:
                    queue.task_done()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(handler_error(symbol, e))
                queue.task_done()

        log(handler_stopped(symbol, trade_key))

    def _validate_signal_for_processing(self, signal_data: dict) -> tuple[bool, str]:
        """Проверяет, что сигнал всё ещё актуален перед запуском сделки."""
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        trade_type = getattr(self.strategy, "_trade_type", "sprint")

        if trade_type == "classic":
            return self.strategy._is_signal_valid_for_classic(
                signal_data,
                current_time,
                for_placement=True,
            )

        return self.strategy._is_signal_valid_for_sprint(signal_data, current_time)

    async def _handle_pending_signal(self, trade_key: str, signal_data: dict):
        """Обрабатывает отложенный сигнал"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log

        if trade_key not in self._pending_signals:
            self._pending_signals[trade_key] = asyncio.Queue(maxsize=1)  # Только 1 слот!
        
        # Очищаем очередь и кладём только последний сигнал
        while not self._pending_signals[trade_key].empty():
            try:
                self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except asyncio.QueueEmpty:
                break
        
        # Если очередь полная, заменяем старый сигнал
        try:
            self._pending_signals[trade_key].put_nowait(signal_data)
        except asyncio.QueueFull:
            try:
                self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except asyncio.QueueEmpty:
                pass
            self._pending_signals[trade_key].put_nowait(signal_data)

        log(signal_deferred(symbol))

        if trade_key not in self._pending_processing:
            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

    def discard_signals_for(self, trade_key: str) -> int:
        """Удаляет сигналы из очередей для указанного ключа."""
        removed = 0

        queue = self._signal_queues.get(trade_key)
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                    removed += 1
                except asyncio.QueueEmpty:
                    break

        pending = self._pending_signals.get(trade_key)
        if pending is not None:
            while not pending.empty():
                try:
                    pending.get_nowait()
                    pending.task_done()
                    removed += 1
                except asyncio.QueueEmpty:
                    break

        return removed

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложку после завершения сделки - ТОЛЬКО ПОСЛЕДНИЙ СИГНАЛ"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)
        
        try:
            if not allow_parallel:
                # Для непараллельного режима - обрабатываем только один отложенный сигнал
                async with self._global_trade_lock:
                    log(global_lock_acquired(symbol))
                    await self._process_one_pending(trade_key)
                    log(global_lock_released(symbol))
            else:
                # Для параллельного режима - ждём завершения активной сделки
                wait_start = asyncio.get_event_loop().time()
                while self._active_trades.get(trade_key) and self.strategy._running:
                    if asyncio.get_event_loop().time() - wait_start > 60.0:
                        break
                    await asyncio.sleep(0.1)

                # Дополнительно ждём, пока стратегия освободит серию (для Мартингейла и др.)
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
        """Обрабатывает один отложенный сигнал"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log
        
        # ПРОВЕРКА ЛИМИТА ДЛЯ ОТЛОЖЕННЫХ СИГНАЛОВ
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
        
        last_signal = None
        while True:
            try:
                last_signal = self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except asyncio.QueueEmpty:
                break
        
        if last_signal:
            signal_symbol = last_signal.get('symbol') or symbol
            is_valid, reason = self._validate_signal_for_processing(last_signal)
            if not is_valid:
                log(deferred_signal_outdated(signal_symbol, reason))
                return

            log(deferred_signal_start(signal_symbol))

            acquired = await try_acquire_trade_slot()
            if not acquired:
                max_trades = get_max_open_trades()
                log(strategy_limit_deferred(signal_symbol, max_trades, get_current_open_trades()))

                queue = self._pending_signals.setdefault(trade_key, asyncio.Queue(maxsize=1))
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(last_signal)
                self._reschedule_pending_processing(trade_key)
                return

            if not self.strategy.params.get("allow_parallel_trades", True):
                try:
                    task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
                    await task  # Ждём
                finally:
                    await release_trade_slot()
            else:
                task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
                tasks = self._active_trades.setdefault(trade_key, set())
                tasks.add(task)

                def cleanup(fut):
                    task_set = self._active_trades.get(trade_key)
                    if task_set is not None:
                        task_set.discard(fut)
                        if not task_set:
                            self._active_trades.pop(trade_key, None)
                    asyncio.create_task(release_trade_slot())

                task.add_done_callback(cleanup)

    async def _check_more_pending_signals(self, trade_key: str):
        """Проверяет наличие дополнительных отложенных сигналов"""
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            symbol, _ = trade_key.split('_', 1)
            log = self.log
            log(pending_signals_restart(symbol))

            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )

    def _reschedule_pending_processing(self, trade_key: str, delay: float = 0.5) -> None:
        """Планирует повторную обработку отложенных сигналов, когда освободится слот."""

        async def _schedule():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            if not self.strategy._running:
                return

            queue = self._pending_signals.get(trade_key)
            if queue is None or queue.empty():
                return

            if trade_key in self._pending_processing:
                return

            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

        asyncio.create_task(_schedule())

    def pop_latest_signal(self, trade_key: str) -> Optional[dict]:
        """Возвращает и удаляет самый свежий отложенный сигнал для ключа сделки."""
        queue = self._pending_signals.get(trade_key)
        if queue is None or queue.empty():
            return None

        latest_signal = None
        while True:
            try:
                latest_signal = queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

        return latest_signal

    def stop(self):
        """Остановка с очисткой всех очередей и задач"""
        # Отменяем все задачи
        all_tasks = []
        all_tasks.extend(self._signal_processors.values())
        all_tasks.extend(self._pending_processing.values())
        for task_set in self._active_trades.values():
            all_tasks.extend(task_set)
        
        for task in all_tasks:
            if not task.done():
                task.cancel()
        
        # Очищаем все очереди
        for queue in list(self._signal_queues.values()):
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
        
        for queue in list(self._pending_signals.values()):
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
        
        self._signal_queues.clear()
        self._signal_processors.clear()
        self._pending_signals.clear()
        self._pending_processing.clear()
        self._active_trades.clear()
