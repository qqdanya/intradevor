# strategy_common.py
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
from core.policy import can_open_new_trade, get_max_open_trades
from strategies.constants import MOSCOW_TZ


class StrategyCommon:
    """Общая логика для всех стратегий с системой очередей — БЕЗ СПАМА"""

    def __init__(self, strategy_instance):
        self.strategy = strategy_instance
        self.log = strategy_instance.log or (lambda s: None)

        # Очереди и задачи
        self._signal_queues: Dict[str, asyncio.Queue] = {}
        self._signal_processors: Dict[str, asyncio.Task] = {}
        self._pending_signals: Dict[str, asyncio.Queue] = {}
        self._pending_processing: Dict[str, asyncio.Task] = {}
        self._active_trades: Dict[str, asyncio.Task] = {}

        # Глобальная блокировка (только при allow_parallel=False)
        self._global_trade_lock = asyncio.Lock()

        # Счётчик открытых сделок (для лимита)
        self._total_open_trades = 0

    async def signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель — кладёт в нужную очередь по trade_key"""
        log = self.log
        strategy_name = getattr(self.strategy, 'strategy_name', 'Strategy')
        log(f"[*] Запуск прослушивателя сигналов ({strategy_name})")

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
                    is_valid, reason = self.strategy._is_signal_valid_for_classic(
                        {'timestamp': signal_timestamp, 'next_expire': next_expire},
                        current_time
                    )
                    if not is_valid:
                        log(f"[{symbol}] ⏰ Сигнал неактуален для classic: {reason} -> пропуск")
                        continue
                else:
                    is_valid, reason = self.strategy._is_signal_valid_for_sprint(
                        {'timestamp': signal_timestamp},
                        current_time
                    )
                    if not is_valid:
                        log(f"[{symbol}] ⏰ Сигнал неактуален для sprint: {reason} -> пропуск")
                        continue

                signal_data = {
                    'direction': direction,
                    'version': ver,
                    'meta': meta,
                    'symbol': meta.get('symbol') if meta else self.strategy.symbol,
                    'timeframe': meta.get('timeframe') if meta else self.strategy.timeframe,
                    'timestamp': signal_timestamp,
                    'indicator': meta.get('indicator') if meta else '-',
                    'next_expire': next_expire,
                }

                symbol = signal_data['symbol']
                timeframe = signal_data['timeframe']
                trade_key = f"{symbol}_{timeframe}"

                self.strategy._last_signal_ver = ver
                self.strategy._last_signal_at_str = signal_timestamp.strftime("%d.%m.%Y %H:%M:%S")

                # Создаём очередь если её нет
                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )

                await self._signal_queues[trade_key].put(signal_data)
                next_time_str = next_expire.strftime('%H:%M:%S') if next_expire else '?'
                log(f"[{symbol}] Сигнал добавлен: свеча {signal_timestamp.strftime('%H:%M:%S')} (до {next_time_str})")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[*] Ошибка в прослушивателе: {e}")
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает очередь — с глобальной блокировкой при allow_parallel=False"""
        queue = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)
        log(f"[{symbol}] Запуск обработчика очереди {trade_key} (allow_parallel={allow_parallel})")

        while self.strategy._running:
            await self.strategy._pause_point()
            try:
                signal_data = await queue.get()

                # ПРОВЕРКА ЛИМИТА ОТКРЫТЫХ СДЕЛОК
                if not can_open_new_trade(self._total_open_trades):
                    max_trades = get_max_open_trades()
                    log(f"[{symbol}] ⚠ Достигнут лимит {max_trades} открытых сделок. Сигнал отложен.")
                    await self._handle_pending_signal(trade_key, signal_data)
                    queue.task_done()
                    continue

                if not allow_parallel:
                    if self._global_trade_lock.locked():
                        await self._handle_pending_signal(trade_key, signal_data)
                        queue.task_done()
                        continue

                    async with self._global_trade_lock:
                        log(f"[{symbol}] Получена глобальная блокировка, начало обработки")
                        self._total_open_trades += 1
                        try:
                            task = asyncio.create_task(self.strategy._process_single_signal(signal_data))
                            await task
                        finally:
                            self._total_open_trades = max(0, self._total_open_trades - 1)
                        log(f"[{symbol}] Освобождение глобальной блокировки")

                else:
                    if trade_key in self._active_trades:
                        await self._handle_pending_signal(trade_key, signal_data)
                    else:
                        self._total_open_trades += 1
                        task = asyncio.create_task(self.strategy._process_single_signal(signal_data))
                        self._active_trades[trade_key] = task

                        def cleanup(fut):
                            self._active_trades.pop(trade_key, None)
                            self._total_open_trades = max(0, self._total_open_trades - 1)
                            queue.task_done()
                            asyncio.create_task(self._check_more_pending_signals(trade_key))

                        task.add_done_callback(cleanup)

                queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{symbol}] Ошибка в обработчике: {e}")
                queue.task_done()

        log(f"[{symbol}] Остановка обработчика {trade_key}")

    async def _handle_pending_signal(self, trade_key: str, signal_data: dict):
        """Обрабатывает отложенный сигнал — только последний"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log

        # Создаём очередь (maxsize=1) — хранит только последний сигнал
        if trade_key not in self._pending_signals:
            self._pending_signals[trade_key] = asyncio.Queue(maxsize=1)

        # Заменяем старый сигнал новым
        while not self._pending_signals[trade_key].empty():
            try:
                self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except asyncio.QueueEmpty:
                break

        try:
            self._pending_signals[trade_key].put_nowait(signal_data)
        except asyncio.QueueFull:
            # Если полная — вытесняем старый
            try:
                self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except:
                pass
            self._pending_signals[trade_key].put_nowait(signal_data)

        log(f"[{symbol}] Сигнал отложен (активная сделка)")

        # Запускаем обработку отложенных только если её ещё нет
        if trade_key not in self._pending_processing:
            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложенный сигнал ПОСЛЕ завершения активной сделки"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)

        try:
            if not allow_parallel:
                async with self._global_trade_lock:
                    await self._process_one_pending(trade_key)
            else:
                # Ждём освобождения слота — но не чаще 1 раза в секунду
                wait_start = asyncio.get_event_loop().time()
                while (trade_key in self._active_trades and
                       self.strategy._running and
                       asyncio.get_event_loop().time() - wait_start < 30.0):
                    await asyncio.sleep(1.0)  # ← Тихо ждём

                if not self.strategy._running:
                    return

                await self._process_one_pending(trade_key)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[{symbol}] Ошибка в отложке: {e}")
        finally:
            self._pending_processing.pop(trade_key, None)

    async def _process_one_pending(self, trade_key: str):
        """Обрабатывает ОДИН отложенный сигнал — с проверкой активной серии"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log

        # КРИТИЧНАЯ ПРОВЕРКА: НЕ ЗАПУСКАТЬ, если серия Martingale активна
        if (hasattr(self.strategy, '_active_series') and
            self.strategy._active_series.get(trade_key, False)):
            log(f"[{symbol}] Активная серия Martingale — отложенный сигнал игнорируется.")
            return

        if not can_open_new_trade(self._total_open_trades):
            max_trades = get_max_open_trades()
            log(f"[{symbol}] Лимит {max_trades} сделок — отложенный сигнал пропущен.")
            return

        if trade_key not in self._pending_signals or self._pending_signals[trade_key].empty():
            return

        # Берём последний сигнал
        last_signal = None
        while True:
            try:
                last_signal = self._pending_signals[trade_key].get_nowait()
                self._pending_signals[trade_key].task_done()
            except asyncio.QueueEmpty:
                break

        if last_signal:
            log(f"[{symbol}] Запуск отложенного сигнала")  # ← Только здесь!
            self._total_open_trades += 1
            task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
            self._active_trades[trade_key] = task

            def cleanup(fut):
                self._active_trades.pop(trade_key, None)
                self._total_open_trades = max(0, self._total_open_trades - 1)
                asyncio.create_task(self._check_more_pending_signals(trade_key))

            task.add_done_callback(cleanup)

    async def _check_more_pending_signals(self, trade_key: str):
        """Перезапускает обработку, если остались отложенные"""
        if (trade_key in self._pending_signals and
            not self._pending_signals[trade_key].empty() and
            trade_key not in self._pending_processing):
            symbol, _ = trade_key.split('_', 1)
            self.log(f"[{symbol}] Есть отложенные — перезапуск")
            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

    def stop(self):
        """Полная остановка с очисткой"""
        self._total_open_trades = 0

        all_tasks = (
            list(self._signal_processors.values()) +
            list(self._pending_processing.values()) +
            list(self._active_trades.values())
        )

        for task in all_tasks:
            if not task.done():
                task.cancel()

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
