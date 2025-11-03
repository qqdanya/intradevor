from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account

ANTI_MARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 3,
    "repeat_count": 10,
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


class AntiMartingaleStrategy(BaseTradingStrategy):
    """Стратегия Антимартингейла (увеличиваем ставку после выигрыша) с системой очередей"""
    
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
        # Объединяем параметры по умолчанию
        anti_martingale_params = dict(ANTI_MARTINGALE_DEFAULTS)
        if params:
            anti_martingale_params.update(params)
            
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=anti_martingale_params,
            strategy_name="AntiMartingale",
            **kwargs,
        )

        # Очереди и задачи для параллельной обработки
        self._signal_queues: Dict[str, asyncio.Queue] = {}
        self._signal_processors: Dict[str, asyncio.Task] = {}
        self._pending_signals: Dict[str, asyncio.Queue] = {}
        self._pending_processing: Dict[str, asyncio.Task] = {}
        self._active_trades: Dict[str, asyncio.Task] = {}

        # Глобальная блокировка — только одна сделка в системе
        self._global_trade_lock = asyncio.Lock()

    async def _signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель — кладёт в нужную очередь по trade_key"""
        log = self.log or (lambda s: None)
        log(f"[*] Запуск прослушивателя сигналов (AntiMartingale)")

        while self._running:
            await self._pause_point()

            try:
                direction, ver, meta = await self._fetch_signal_payload(self._last_signal_ver)

                # === ИЗВЛЕКАЕМ timestamp (время свечи) и next_timestamp ===
                signal_timestamp = datetime.now(ZoneInfo(MOSCOW_TZ))
                next_expire = None

                if meta and isinstance(meta, dict):
                    ts_raw = meta.get('timestamp')
                    if ts_raw and isinstance(ts_raw, datetime):
                        signal_timestamp = ts_raw.astimezone(ZoneInfo(MOSCOW_TZ))
                    
                    next_raw = meta.get('next_timestamp')
                    if next_raw and isinstance(next_raw, datetime):
                        next_expire = next_raw.astimezone(ZoneInfo(MOSCOW_TZ))

                signal_data = {
                    'direction': direction,
                    'version': ver,
                    'meta': meta,
                    'symbol': meta.get('symbol') if meta else self.symbol,
                    'timeframe': meta.get('timeframe') if meta else self.timeframe,
                    'timestamp': signal_timestamp,
                    'indicator': meta.get('indicator') if meta else '-',
                    'next_expire': next_expire,
                }

                symbol = signal_data['symbol']
                timeframe = signal_data['timeframe']
                trade_key = f"{symbol}_{timeframe}"

                self._last_signal_ver = ver
                self._last_signal_at_str = signal_timestamp.strftime("%d.%m.%Y %H:%M:%S")

                # Создаём очередь
                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )

                await self._signal_queues[trade_key].put(signal_data)
                log(f"[{symbol}] Сигнал добавлен: свеча {signal_timestamp.strftime('%H:%M:%S')}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[*] Ошибка в прослушивателе: {e}")
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает очередь — с глобальной блокировкой при allow_parallel=False"""
        queue = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log or (lambda s: None)
        allow_parallel = self.params.get("allow_parallel_trades", True)

        log(f"[{symbol}] Запуск обработчика очереди {trade_key} (allow_parallel={allow_parallel})")

        while self._running:
            await self._pause_point()

            try:
                signal_data = await queue.get()

                if not allow_parallel:
                    # === ГЛОБАЛЬНАЯ БЛОКИРОВКА ДЛЯ ВСЕХ СИМВОЛОВ ===
                    if self._global_trade_lock.locked():
                        # ЗАМЕНА: вместо очереди просто заменяем последний отложенный сигнал
                        if trade_key not in self._pending_signals:
                            self._pending_signals[trade_key] = asyncio.Queue(maxsize=1)  # Только 1 слот!
                        
                        # Очищаем очередь и кладём только последний сигнал
                        while not self._pending_signals[trade_key].empty():
                            try:
                                self._pending_signals[trade_key].get_nowait()
                                self._pending_signals[trade_key].task_done()
                            except asyncio.QueueEmpty:
                                break
                        
                        # Если очередь полная, это значит есть старый сигнал - заменяем его
                        try:
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        except asyncio.QueueFull:
                            # Удаляем старый и кладём новый
                            try:
                                self._pending_signals[trade_key].get_nowait()
                                self._pending_signals[trade_key].task_done()
                            except asyncio.QueueEmpty:
                                pass
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        
                        log(f"[{symbol}] Сигнал отложен (идёт другая сделка в системе)")

                        if trade_key not in self._pending_processing:
                            self._pending_processing[trade_key] = asyncio.create_task(
                                self._process_pending_signals(trade_key)
                            )
                        queue.task_done()
                        continue

                    # Блокируем и обрабатываем - ОДНА сделка на всю систему
                    async with self._global_trade_lock:
                        log(f"[{symbol}] Получена глобальная блокировка, начало обработки")
                        task = asyncio.create_task(self._process_single_signal(signal_data))
                        await task  # Ждём завершения ПОД блокировкой
                        log(f"[{symbol}] Освобождение глобальной блокировки")

                else:
                    # === ПАРАЛЛЕЛЬНЫЕ СДЕЛКИ ===
                    if trade_key in self._active_trades:
                        if trade_key not in self._pending_signals:
                            self._pending_signals[trade_key] = asyncio.Queue()

                        while not self._pending_signals[trade_key].empty():
                            try:
                                self._pending_signals[trade_key].get_nowait()
                                self._pending_signals[trade_key].task_done()
                            except asyncio.QueueEmpty:
                                break

                        await self._pending_signals[trade_key].put(signal_data)
                        log(f"[{symbol}] Сигнал отложен (активная сделка по этому символу)")

                        if trade_key not in self._pending_processing:
                            self._pending_processing[trade_key] = asyncio.create_task(
                                self._process_pending_signals(trade_key)
                            )
                    else:
                        task = asyncio.create_task(self._process_single_signal(signal_data))
                        self._active_trades[trade_key] = task

                        def cleanup(fut):
                            self._active_trades.pop(trade_key, None)
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

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложку после завершения сделки - ТОЛЬКО ПОСЛЕДНИЙ СИГНАЛ"""
        symbol, _ = trade_key.split('_', 1)
        log = self.log or (lambda s: None)
        allow_parallel = self.params.get("allow_parallel_trades", True)

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
                while trade_key in self._active_trades and self._running:
                    if asyncio.get_event_loop().time() - wait_start > 60.0:
                        break
                    await asyncio.sleep(0.1)
                if not self._running:
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
        log = self.log or (lambda s: None)

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
            task = asyncio.create_task(self._process_single_signal(last_signal))
            if not self.params.get("allow_parallel_trades", True):
                await task  # Ждём
            else:
                self._active_trades[trade_key] = task
                task.add_done_callback(lambda f: self._active_trades.pop(trade_key, None))

    async def _check_more_pending_signals(self, trade_key: str):
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            symbol, _ = trade_key.split('_', 1)
            log = self.log or (lambda s: None)
            log(f"[{symbol}] Есть отложенные — перезапуск")
            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Антимартингейла"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала (Антимартингейл)")
        
        # Обновляем информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")
        
        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        # Обновляем символ и таймфрейм если используются "все"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        # Проверяем актуальность сигнала перед началом серии
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        max_age = self._max_signal_age_seconds()
        
        if max_age > 0:
            deadline = signal_data['timestamp'] + timedelta(seconds=max_age)
            if current_time > deadline:
                log(f"[{symbol}] Сигнал устарел перед началом серии: свеча {signal_data['timestamp'].strftime('%H:%M:%S')} + {max_age}s = {deadline.strftime('%H:%M:%S')}, сейчас {current_time.strftime('%H:%M:%S')}")
                return
        
        # Проверяем окно classic перед началом серии
        if self._trade_type == "classic":
            next_expire = signal_data.get('next_expire')
            if next_expire and current_time >= next_expire:
                log(f"[{symbol}] Окно classic закрыто перед началом серии: {next_expire.strftime('%H:%M:%S')}")
                return

        # Запускаем серию Антимартингейла для этого сигнала
        await self._run_antimartingale_series(symbol, timeframe, direction, log, signal_data['timestamp'], signal_data)

    async def _run_antimartingale_series(self, symbol: str, timeframe: str, initial_direction: int, log, signal_received_time: datetime, signal_data: dict):
        """Запускает серию Антимартингейла для конкретного сигнала"""
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] 🛑 repeat_count={series_left} — нечего выполнять.")
            return

        step = 0
        did_place_any_trade = False
        max_steps = int(self.params.get("max_steps", 3))
        base_stake = float(self.params.get("base_investment", 100))
        current_stake = base_stake

        while self._running and step < max_steps:
            await self._pause_point()

            if not await self.ensure_account_conditions():
                continue

            # Проверяем возраст сигнала
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            max_age = self._max_signal_age_seconds()
            
            if max_age > 0:
                deadline = signal_received_time + timedelta(seconds=max_age)
                if current_time > deadline:
                    log(f"[{symbol}] Сигнал устарел: свеча {signal_received_time.strftime('%H:%M:%S')} + {max_age}s = {deadline.strftime('%H:%M:%S')}, сейчас {current_time.strftime('%H:%M:%S')}")
                    return

            # Проверяем окно classic
            if self._trade_type == "classic":
                next_expire = signal_data.get('next_expire')
                if next_expire and current_time >= next_expire:
                    log(f"[{symbol}] Окно classic закрыто: {next_expire.strftime('%H:%M:%S')}")
                    return

            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            # Проверяем выплату и баланс
            pct, balance = await self.check_payout_and_balance(symbol, current_stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step} stake={format_amount(current_stake)} min={self._trade_minutes} "
                f"side={'UP' if initial_direction == 1 else 'DOWN'} payout={pct}%")

            # Определяем режим аккаунта
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # Размещаем сделку
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(
                symbol, initial_direction, current_stake, self._anchor_ccy
            )
                    
            if not trade_id:
                log(f"[{symbol}] ❌ Не удалось разместить сделку. Прерываем серию.")
                break

            did_place_any_trade = True

            # Определяем длительность сделки
            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)

            wait_seconds = self.params.get("result_wait_s")
            if wait_seconds is None:
                wait_seconds = trade_seconds
            else:
                wait_seconds = float(wait_seconds)

            # Уведомляем о pending сделке
            self._notify_pending_trade(
                trade_id, symbol, timeframe, initial_direction, current_stake, pct, 
                trade_seconds, account_mode, expected_end_ts
            )

            self._register_pending_trade(trade_id, symbol, timeframe)

            # Ожидаем результат сделки
            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=self._last_signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=initial_direction,
                stake=float(current_stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )

            # Обрабатываем результат по логике Антимартингейла
            if profit is None:
                log(f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                break
            elif profit > 0:
                log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Увеличиваем ставку.")
                # Антимартингейл: увеличиваем ставку на размер выигрыша
                current_stake += float(profit)
                step += 1
                if step >= max_steps:
                    log(f"[{symbol}] 🎯 Достигнут лимит шагов ({max_steps}).")
                    break
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения ставки.")
            else:
                log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Серия завершена.")
                break

            await self.sleep(0.2)

            # Обновляем время экспирации для classic
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(
                    minutes=_minutes_from_timeframe(timeframe)
                )

        if did_place_any_trade:
            series_left -= 1
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки"""
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
        self, trade_id: str, symbol: str, timeframe: str, direction: int, 
        stake: float, percent: int, trade_seconds: float, 
        account_mode: str, expected_end_ts: float
    ):
        """Уведомляет о pending сделке"""
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if callable(self._on_trade_pending):
            try:
                self._on_trade_pending(
                    trade_id=trade_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_at=self._last_signal_at_str,
                    placed_at=placed_at_str,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                )
            except Exception:
                pass

    def stop(self):
        """Остановка стратегии с очисткой всех очередей и задач"""
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

        super().stop()
