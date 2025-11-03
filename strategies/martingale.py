# martingale.py
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from core.signal_waiter import wait_for_signal_versioned, ANY_SYMBOL, ANY_TIMEFRAME

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
    "max_signal_age_sec": 60,  # НОВЫЙ ПАРАМЕТР: максимальный возраст сигнала
}

class MartingaleStrategy(BaseTradingStrategy):
    """Стратегия Мартингейла с системой очередей и параллельной обработкой"""
   
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

        # === ВАЖНО: Время старта стратегии ===
        self._start_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        self._max_signal_age = int(self.params.get("max_signal_age_sec", 60))

        # Очереди и задачи
        self._signal_queues: Dict[str, asyncio.Queue] = {}
        self._signal_processors: Dict[str, asyncio.Task] = {}
        self._pending_signals: Dict[str, asyncio.Queue] = {}
        self._pending_processing: Dict[str, asyncio.Task] = {}
        self._active_trades: Dict[str, asyncio.Task] = {}
        self._global_trade_lock = asyncio.Lock()
    async def _signal_listener(self):
        """Прослушиватель — ждёт НОВЫЕ сигналы через wait_for_signal_versioned"""
        log = self.log or (lambda s: None)
        log(f"[*] Запуск прослушивателя сигналов (Martingale)")

        # Определяем символ и таймфрейм для ожидания
        wait_symbol = ANY_SYMBOL if self._use_any_symbol else self.symbol
        wait_timeframe = ANY_TIMEFRAME if self._use_any_timeframe else self.timeframe

        while self._running:
            await self._pause_point()
            try:
                # Ждём НОВЫЙ сигнал с фильтрацией по возрасту
                direction, ver, meta = await wait_for_signal_versioned(
                    symbol=wait_symbol,
                    timeframe=wait_timeframe,
                    since_version=self._last_signal_ver,
                    timeout=self.params.get("signal_timeout_sec", 300),
                    max_age_sec=self._max_signal_age,
                    include_meta=True,
                    check_pause=self._pause_point,
                )

                # === Формируем данные сигнала ===
                symbol = meta.get('symbol') or self.symbol
                timeframe = meta.get('timeframe') or self.timeframe
                trade_key = f"{symbol}_{timeframe}"

                signal_timestamp = meta.get('timestamp') or datetime.now(ZoneInfo(MOSCOW_TZ))
                next_expire = meta.get('next_timestamp')

                signal_data = {
                    'direction': direction,
                    'version': ver,
                    'meta': meta,
                    'symbol': symbol,
                    'timeframe': timeframe,
                    'timestamp': signal_timestamp,
                    'indicator': meta.get('indicator', '-'),
                    'next_expire': next_expire,
                }

                self._last_signal_ver = ver
                self._last_signal_at_str = signal_timestamp.strftime("%d.%m.%Y %H:%M:%S")

                # Создаём очередь и обработчик
                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )

                await self._signal_queues[trade_key].put(signal_data)
                log(f"[{symbol}] Сигнал добавлен: свеча {signal_timestamp.strftime('%H:%M:%S')}")

            except asyncio.TimeoutError:
                log(f"[*] Таймаут ожидания сигнала ({self.params.get('signal_timeout_sec')}с)")
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[*] Ошибка в прослушивателе: {e}")
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает очередь сигналов"""
        queue = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log or (lambda s: None)
        allow_parallel = self.params.get("allow_parallel_trades", True)
        log(f"[{symbol}] Запуск обработчика очереди {trade_key} (parallel={allow_parallel})")

        while self._running:
            await self._pause_point()
            try:
                signal_data = await queue.get()

                if not allow_parallel:
                    # === Глобальная блокировка ===
                    if self._global_trade_lock.locked():
                        # Отложим только ПОСЛЕДНИЙ сигнал
                        if trade_key not in self._pending_signals:
                            self._pending_signals[trade_key] = asyncio.Queue(maxsize=1)
                        try:
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        except asyncio.QueueFull:
                            self._pending_signals[trade_key].get_nowait()
                            self._pending_signals[trade_key].task_done()
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        log(f"[{symbol}] Сигнал отложен (глобальная блокировка)")
                        if trade_key not in self._pending_processing:
                            self._pending_processing[trade_key] = asyncio.create_task(
                                self._process_pending_signals(trade_key)
                            )
                        queue.task_done()
                        continue

                    async with self._global_trade_lock:
                        await self._process_single_signal(signal_data)

                else:
                    # === Параллельные сделки ===
                    if trade_key in self._active_trades:
                        if trade_key not in self._pending_signals:
                            self._pending_signals[trade_key] = asyncio.Queue()
                        try:
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        except asyncio.QueueFull:
                            self._pending_signals[trade_key].get_nowait()
                            self._pending_signals[trade_key].task_done()
                            self._pending_signals[trade_key].put_nowait(signal_data)
                        log(f"[{symbol}] Сигнал отложен (активная сделка)")
                        if trade_key not in self._pending_processing:
                            self._pending_processing[trade_key] = asyncio.create_task(
                                self._process_pending_signals(trade_key)
                            )
                    else:
                        task = asyncio.create_task(self._process_single_signal(signal_data))
                        self._active_trades[trade_key] = task
                        task.add_done_callback(lambda f: (
                            self._active_trades.pop(trade_key, None),
                            queue.task_done(),
                            asyncio.create_task(self._check_more_pending_signals(trade_key))
                        ))

                queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{symbol}] Ошибка в обработчике: {e}")
                queue.task_done()

        log(f"[{symbol}] Остановка обработчика {trade_key}")

    async def _process_pending_signals(self, trade_key: str):
        symbol, _ = trade_key.split('_', 1)
        log = self.log or (lambda s: None)
        allow_parallel = self.params.get("allow_parallel_trades", True)

        try:
            if not allow_parallel:
                async with self._global_trade_lock:
                    await self._process_one_pending(trade_key)
            else:
                while trade_key in self._active_trades and self._running:
                    await asyncio.sleep(0.1)
                if self._running:
                    await self._process_one_pending(trade_key)
        except Exception as e:
            log(f"[{symbol}] Ошибка обработки отложки: {e}")
        finally:
            self._pending_processing.pop(trade_key, None)

    async def _process_one_pending(self, trade_key: str):
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
            task = asyncio.create_task(self._process_single_signal(last_signal))
            if not self.params.get("allow_parallel_trades", True):
                await task
            else:
                self._active_trades[trade_key] = task
                task.add_done_callback(lambda f: self._active_trades.pop(trade_key, None))

    async def _check_more_pending_signals(self, trade_key: str):
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала (Мартингейл)")

        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")

        ts = signal_data['meta'].get('next_timestamp')
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = timeframe

        # === Проверка окна classic ===
        if self._trade_type == "classic" and self._next_expire_dt:
            if datetime.now(ZoneInfo(MOSCOW_TZ)) >= self._next_expire_dt:
                log(f"[{symbol}] Окно classic закрыто: {self._next_expire_dt.strftime('%H:%M:%S')}")
                return

        await self._run_martingale_series(
            symbol, timeframe, direction, log,
            signal_data['timestamp'], signal_data
        )

    async def _run_martingale_series(self, symbol: str, timeframe: str, initial_direction: int,
                                   log, signal_received_time: datetime, signal_data: dict):
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] repeat_count=0 — пропуск.")
            return

        step = 0
        did_place_any_trade = False
        max_steps = int(self.params.get("max_steps", 5))

        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

            # Проверка окна classic
            if self._trade_type == "classic" and self._next_expire_dt:
                if current_time >= self._next_expire_dt:
                    log(f"[{symbol}] Окно classic закрыто: {self._next_expire_dt.strftime('%H:%M:%S')}")
                    return

            base_stake = float(self.params.get("base_investment", 100))
            coeff = float(self.params.get("coefficient", 2.0))
            stake = base_stake * (coeff ** step) if step > 0 else base_stake
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step} stake={format_amount(stake)} side={'UP' if initial_direction==1 else 'DOWN'} payout={pct}%")

            demo_now = await is_demo_account(self.http_client)
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, initial_direction, stake, self._anchor_ccy)
            if not trade_id:
                log(f"[{symbol}] Не удалось разместить сделку.")
                break

            did_place_any_trade = True
            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = float(self.params.get("result_wait_s", trade_seconds))

            self._notify_pending_trade(
                trade_id, symbol, timeframe, initial_direction, stake, pct,
                trade_seconds, account_mode, expected_end_ts
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=wait_seconds,
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=self._last_signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=initial_direction,
                stake=stake,
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )

            if profit is None:
                log(f"[{symbol}] Результат неизвестен → LOSS.")
                step += 1
            elif profit > 0:
                log(f"[{symbol}] WIN: +{format_amount(profit)}. Серия завершена.")
                break
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] PUSH: возврат. Повтор без удвоения.")
            else:
                log(f"[{symbol}] LOSS: {format_amount(profit)}. Удваиваем.")
                step += 1

            await self.sleep(0.2)

            if self._trade_type == "classic" and self._next_expire_dt:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))

        if did_place_any_trade:
            series_left -= 1
            log(f"[{symbol}] Осталось серий: {series_left}")
            if step >= max_steps:
                log(f"[{symbol}] Достигнут лимит шагов ({max_steps}).")

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(0.0, (self._next_expire_dt - datetime.now(ZoneInfo(MOSCOW_TZ))).total_seconds())
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds
        return trade_seconds, expected_end_ts

    def _notify_pending_trade(self, trade_id: str, symbol: str, timeframe: str, direction: int,
                            stake: float, percent: int, trade_seconds: float,
                            account_mode: str, expected_end_ts: float):
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
        all_tasks = list(self._signal_processors.values()) + \
                    list(self._pending_processing.values()) + \
                    list(self._active_trades.values())
        for task in all_tasks:
            if not task.done():
                task.cancel()

        for queue in list(self._signal_queues.values()) + list(self._pending_signals.values()):
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
