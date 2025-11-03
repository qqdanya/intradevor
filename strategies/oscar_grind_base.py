# oscar_grind_base.py
from __future__ import annotations
import asyncio
import math
from datetime import datetime, timedelta
from typing import Optional, Dict, Set
from zoneinfo import ZoneInfo
from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account

OSCAR_GRIND_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 20,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "double_entry": True,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}

class OscarGrindBaseStrategy(BaseTradingStrategy):
    """Базовая стратегия Oscar Grind — с полной параллельной обработкой по trade_key"""

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
        strategy_name: str = "OscarGrind",
        **kwargs,
    ):
        oscar_params = dict(OSCAR_GRIND_DEFAULTS)
        if params:
            oscar_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=oscar_params,
            strategy_name=strategy_name,
            **kwargs,
        )

        # === НОВЫЕ СТРУКТУРЫ: по trade_key ===
        self._signal_queues: Dict[str, asyncio.Queue] = {}      # trade_key -> очередь сигналов
        self._signal_processors: Dict[str, asyncio.Task] = {}   # trade_key -> обработчик очереди
        self._pending_signals: Dict[str, asyncio.Queue] = {}    # trade_key -> отложенные сигналы
        self._pending_processing: Dict[str, asyncio.Task] = {}  # trade_key -> обработчик отложки
        self._pending_notified: Set[str] = set()

        # Убираем перезапись self.symbol — она больше не нужна
        # self.symbol и self.timeframe — только для логов и инициализации

    async def _signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель сигналов — кладёт в нужную очередь по trade_key"""
        log = self.log or (lambda s: None)
        log(f"[*] Запуск прослушивателя сигналов ({self.strategy_name})")

        while self._running:
            await self._pause_point()

            try:
                direction, ver, meta = await self._fetch_signal_payload(self._last_signal_ver)

                signal_data = {
                    'direction': direction,
                    'version': ver,
                    'meta': meta,
                    'symbol': meta.get('symbol') if meta else self.symbol,
                    'timeframe': meta.get('timeframe') if meta else self.timeframe,
                    'timestamp': datetime.now(),
                    'indicator': meta.get('indicator') if meta else '-'
                }

                symbol = signal_data['symbol']
                timeframe = signal_data['timeframe']
                trade_key = f"{symbol}_{timeframe}"

                # Обновляем версию
                self._last_signal_ver = ver
                self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")

                # Создаём очередь и обработчик, если ещё нет
                if trade_key not in self._signal_queues:
                    self._signal_queues[trade_key] = asyncio.Queue()
                    self._signal_processors[trade_key] = asyncio.create_task(
                        self._process_signal_queue(trade_key)
                    )
                    log(f"[{symbol}] Создана очередь и обработчик для {trade_key}")

                # Кладём сигнал в свою очередь
                await self._signal_queues[trade_key].put(signal_data)
                log(f"[{symbol}] Сигнал добавлен в очередь {trade_key}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[*] Ошибка в прослушивателе сигналов: {e}")
                await asyncio.sleep(1.0)

    async def _process_signal_queue(self, trade_key: str):
        """Обрабатывает сигналы из своей очереди (по trade_key)"""
        queue = self._signal_queues[trade_key]
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log or (lambda s: None)

        log(f"[{symbol}] Запуск обработчика очереди {trade_key}")

        while self._running:
            await self._pause_point()

            try:
                signal_data = await queue.get()

                # Проверка: есть активная сделка?
                if trade_key in self._active_trades:
                    # Откладываем
                    if trade_key not in self._pending_signals:
                        self._pending_signals[trade_key] = asyncio.Queue()
                        log(f"[{symbol}] Создана отложенная очередь")

                    # Очищаем старые сигналы — оставляем только последний
                    while not self._pending_signals[trade_key].empty():
                        try:
                            self._pending_signals[trade_key].get_nowait()
                            self._pending_signals[trade_key].task_done()
                        except asyncio.QueueEmpty:
                            break

                    await self._pending_signals[trade_key].put(signal_data)
                    log(f"[{symbol}] Сигнал отложен (активная сделка)")

                    # Запускаем обработчик отложки
                    if trade_key not in self._pending_processing:
                        self._pending_processing[trade_key] = asyncio.create_task(
                            self._process_pending_signals(trade_key)
                        )

                else:
                    # Обрабатываем немедленно
                    task = asyncio.create_task(self._process_single_signal(signal_data))
                    self._active_trades[trade_key] = task

                    def cleanup(fut):
                        self._active_trades.pop(trade_key, None)
                        queue.task_done()
                        # После завершения — проверяем отложку
                        asyncio.create_task(self._check_more_pending_signals(trade_key))

                    task.add_done_callback(cleanup)

                queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{symbol}] Ошибка в обработчике очереди: {e}")
                queue.task_done()

        log(f"[{symbol}] Остановка обработчика очереди {trade_key}")

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложенные сигналы после завершения активной сделки"""
        symbol, timeframe = trade_key.split('_', 1)
        log = self.log or (lambda s: None)

        try:
            while self._running:
                # Ждём завершения активной
                while trade_key in self._active_trades and self._running:
                    await asyncio.sleep(0.1)

                if not self._running:
                    break

                if trade_key not in self._pending_signals or self._pending_signals[trade_key].empty():
                    break

                # Берём последний сигнал
                last_signal = None
                while True:
                    try:
                        last_signal = self._pending_signals[trade_key].get_nowait()
                        self._pending_signals[trade_key].task_done()
                    except asyncio.QueueEmpty:
                        break

                if last_signal:
                    log(f"[{symbol}] Обрабатываем отложенный сигнал")
                    task = asyncio.create_task(self._process_single_signal(last_signal))
                    self._active_trades[trade_key] = task

                    def cleanup(fut):
                        self._active_trades.pop(trade_key, None)
                        asyncio.create_task(self._check_more_pending_signals(trade_key))

                    task.add_done_callback(cleanup)
                    await task

                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[{symbol}] Ошибка в обработчике отложки: {e}")
        finally:
            self._pending_processing.pop(trade_key, None)
            self._pending_notified.discard(trade_key)

    async def _check_more_pending_signals(self, trade_key: str):
        """Проверяет, есть ли ещё отложенные сигналы"""
        if trade_key in self._pending_signals and not self._pending_signals[trade_key].empty():
            symbol, _ = trade_key.split('_', 1)
            log = self.log or (lambda s: None)
            log(f"[{symbol}] Есть отложенные сигналы — перезапускаем обработчик")
            if trade_key not in self._pending_processing:
                self._pending_processing[trade_key] = asyncio.create_task(
                    self._process_pending_signals(trade_key)
                )
        else:
            if trade_key in self._pending_notified:
                symbol, _ = trade_key.split('_', 1)
                log = self.log or (lambda s: None)
                log(f"[{symbol}] Все отложенные сигналы обработаны")
                self._pending_notified.discard(trade_key)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        log = self.log or (lambda s: None)

        log(f"[{symbol}] Начало обработки сигнала Oscar Grind")
        self._last_indicator = signal_data['indicator']

        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        signal_received_time = signal_data['timestamp'].astimezone(ZoneInfo(MOSCOW_TZ))
        await self._run_oscar_grind_series(symbol, timeframe, direction, log, signal_received_time)

    async def _run_oscar_grind_series(self, symbol: str, timeframe: str, initial_direction: int, log, signal_received_time: datetime):
        # ... (без изменений, кроме логов)
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] repeat_count={series_left} — нечего выполнять.")
            return

        base_unit = float(self.params.get("base_investment", 100))
        target_profit = base_unit
        max_steps = int(self.params.get("max_steps", 20))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        double_entry = bool(self.params.get("double_entry", True))

        if max_steps <= 0:
            log(f"[{symbol}] max_steps={max_steps} — серию не стартуем.")
            return

        step_idx = 0
        cum_profit = 0.0
        stake = base_unit
        series_started = False
        series_direction = initial_direction
        repeat_trade = False

        while self._running and step_idx < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            max_age = self._max_signal_age_seconds()
            if max_age > 0:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                signal_age = (current_time - signal_received_time).total_seconds()
                if signal_age > max_age:
                    log(f"[{symbol}] Сигнал устарел ({signal_age:.1f}s > {max_age:.0f}s). Прерываем серию.")
                    break

            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step_idx + 1} stake={format_amount(stake)} min={self._trade_minutes} "
                f"side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, stake, self._anchor_ccy)
            if not trade_id:
                log(f"[{symbol}] Не удалось разместить сделку. Пропускаем шаг.")
                await asyncio.sleep(2.0)
                continue

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s", trade_seconds)

            self._notify_pending_trade(
                trade_id, symbol, timeframe, series_direction, stake, pct,
                trade_seconds, account_mode, expected_end_ts
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=self._last_signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )

            if profit is None:
                profit_val = -float(stake)
                outcome = "loss"
            else:
                profit_val = float(profit)
                outcome = "win" if profit_val > 0 else "refund" if profit_val == 0 else "loss"

            if not series_started:
                if outcome == "loss":
                    series_started = True
                    cum_profit += profit_val
                else:
                    stake = base_unit
                    continue
            else:
                cum_profit += profit_val

            if cum_profit >= target_profit:
                log(f"[{symbol}] Серия завершена: цель {format_amount(target_profit)} достигнута.")
                step_idx += 1
                break

            need = max(0.0, target_profit - cum_profit)
            next_stake = self._next_stake(
                outcome=outcome,
                stake=stake,
                base_unit=base_unit,
                pct=pct,
                need=need,
                profit=profit_val,
                cum_profit=cum_profit,
                log=log,
            )
            stake = float(next_stake)
            step_idx += 1

            if repeat_trade:
                repeat_trade = False
                series_direction = None
            else:
                if double_entry and outcome == "loss":
                    repeat_trade = True
                else:
                    series_direction = None

            await self.sleep(0.2)

            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))

        if step_idx > 0:
            series_left -= 1
            log(f"[{symbol}] Осталось серий: {series_left}")

    def _next_stake(self, *, outcome: str, stake: float, base_unit: float, pct: float, need: float, profit: float, cum_profit: float, log) -> float:
        raise NotImplementedError("Реализуйте в дочернем классе")

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
        """Остановка с очисткой"""
        for task in self._signal_processors.values():
            task.cancel()
        for task in self._pending_processing.values():
            task.cancel()

        self._signal_queues.clear()
        self._signal_processors.clear()
        self._pending_signals.clear()
        self._pending_processing.clear()
        self._pending_notified.clear()

        super().stop()
