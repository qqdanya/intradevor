from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
from core.policy import can_open_new_trade, get_max_open_trades  # Добавляем импорт
from strategies.constants import MOSCOW_TZ

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
        self._active_trades: Dict[str, asyncio.Task] = {}
        
        # Глобальная блокировка — только одна сделка в системе
        self._global_trade_lock = asyncio.Lock()
        
        # Добавляем отслеживание общего количества открытых сделок
        self._total_open_trades = 0  # Общий счетчик для всех символов/таймфреймов

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
                        log(f"[{symbol}] ⏰ Сигнал неактуален для classic: {reason} -> пропуск")
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
                    # Глобальная блокировка для всех символов
                    if self._global_trade_lock.locked():
                        await self._handle_pending_signal(trade_key, signal_data)
                        queue.task_done()
                        continue
                    
                    # Блокируем и обрабатываем - ОДНА сделка на всю систему
                    async with self._global_trade_lock:
                        log(f"[{symbol}] Получена глобальная блокировка, начало обработки")
                        self._total_open_trades += 1  # Увеличиваем счетчик
                        try:
                            task = asyncio.create_task(self.strategy._process_single_signal(signal_data))
                            await task  # Ждём завершения ПОД блокировкой
                        finally:
                            self._total_open_trades = max(0, self._total_open_trades - 1)  # Уменьшаем счетчик
                        log(f"[{symbol}] Освобождение глобальной блокировки")
                        
                else:
                    # Параллельные сделки
                    if trade_key in self._active_trades:
                        await self._handle_pending_signal(trade_key, signal_data)
                    else:
                        self._total_open_trades += 1  # Увеличиваем счетчик
                        task = asyncio.create_task(self.strategy._process_single_signal(signal_data))
                        self._active_trades[trade_key] = task
                        
                        def cleanup(fut):
                            self._active_trades.pop(trade_key, None)
                            self._total_open_trades = max(0, self._total_open_trades - 1)  # Уменьшаем счетчик
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
        
        log(f"[{symbol}] Сигнал отложен (активная сделка)")
        
        if trade_key not in self._pending_processing:
            self._pending_processing[trade_key] = asyncio.create_task(
                self._process_pending_signals(trade_key)
            )

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложку после завершения сделки - ТОЛЬКО ПОСЛЕДНИЙ СИГНАЛ"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log
        allow_parallel = self.strategy.params.get("allow_parallel_trades", True)
        
        try:
            if not allow_parallel:
                # Для непараллельного режима - обрабатываем только один отложенный сигнал
                async with self._global_trade_lock:
                    log(f"[{symbol}] Получена глобальная блокировка для отложенного сигнала")
                    await self._process_one_pending(trade_key)
                    log(f"[{symbol}] Освобождение глобальной блокировки для отложенного сигнала")
            else:
                # Для параллельного режима - ждём завершения активной сделки
                wait_start = asyncio.get_event_loop().time()
                while trade_key in self._active_trades and self.strategy._running:
                    if asyncio.get_event_loop().time() - wait_start > 60.0:
                        break
                    await asyncio.sleep(0.1)
                
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
        """Обрабатывает один отложенный сигнал"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log
        
        # ПРОВЕРКА ЛИМИТА ДЛЯ ОТЛОЖЕННЫХ СИГНАЛОВ
        if not can_open_new_trade(self._total_open_trades):
            max_trades = get_max_open_trades()
            log(f"[{symbol}] ⚠ Лимит {max_trades} сделок - отложенный сигнал не обработан")
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
            log(f"[{symbol}] Запуск отложенного сигнала")
            
            if not self.strategy.params.get("allow_parallel_trades", True):
                self._total_open_trades += 1  # Увеличиваем счетчик
                try:
                    task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
                    await task  # Ждём
                finally:
                    self._total_open_trades = max(0, self._total_open_trades - 1)  # Уменьшаем счетчик
            else:
                self._total_open_trades += 1  # Увеличиваем счетчик
                task = asyncio.create_task(self.strategy._process_single_signal(last_signal))
                self._active_trades[trade_key] = task
                
                def cleanup(fut):
                    self._active_trades.pop(trade_key, None)
                    self._total_open_trades = max(0, self._total_open_trades - 1)
                
                task.add_done_callback(cleanup)

    async def _check_more_pending_signals(self, trade_key: str):
        """Проверяет наличие дополнительных отложенных сигналов"""
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            symbol, _ = trade_key.split('_', 1)
            log = self.log
            log(f"[{symbol}] Есть отложенные — перезапуск")
            
            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )

    def stop(self):
        """Остановка с очисткой всех очередей и задач"""
        # Сбрасываем счетчик сделок
        self._total_open_trades = 0
        
        # Отменяем все задачи
        all_tasks = []
        all_tasks.extend(self._signal_processors.values())
        all_tasks.extend(self._pending_processing.values())
        all_tasks.extend(self._active_trades.values())
        
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
