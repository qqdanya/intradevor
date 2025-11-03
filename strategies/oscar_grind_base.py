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
    """Базовая стратегия Oscar Grind"""
    
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
        # Объединяем параметры по умолчанию
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
        
        # Очередь отложенных сигналов по инструментам
        self._pending_signals: Dict[str, asyncio.Queue] = {}  # trade_key -> Queue
        self._pending_processing: Dict[str, asyncio.Task] = {}  # trade_key -> Task
        self._pending_notified: Set[str] = set()

    async def _signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель сигналов с отложенной обработкой"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск прослушивателя сигналов ({self.strategy_name})")
        
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
                
                # ОБНОВЛЯЕМ ВЕРСИЮ СИГНАЛА СРАЗУ ПОСЛЕ ПОЛУЧЕНИЯ - ЭТО КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ!
                self._last_signal_ver = ver
                self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")
                
                # Если для этого инструмента уже есть активная сделка - сохраняем сигнал в отложенную очередь
                if trade_key in self._active_trades:
                    if trade_key not in self._pending_notified:
                        log(f"[{symbol}] ⏳ Активная сделка выполняется, откладываем сигнал для {symbol} {timeframe}")
                        self._pending_notified.add(trade_key)
                    
                    # Создаем или получаем очередь отложенных сигналов для этого инструмента
                    if trade_key not in self._pending_signals:
                        self._pending_signals[trade_key] = asyncio.Queue()
                    
                    # Сохраняем сигнал в отложенную очередь
                    await self._pending_signals[trade_key].put(signal_data)
                    log(f"[{symbol}] 📨 Сигнал сохранен в отложенную очередь (в очереди: {self._pending_signals[trade_key].qsize()})")
                    
                    # Запускаем обработчик отложенных сигналов, если он еще не запущен и есть сигналы
                    if trade_key not in self._pending_processing and not self._pending_signals[trade_key].empty():
                        self._pending_processing[trade_key] = asyncio.create_task(
                            self._process_pending_signals(trade_key)
                        )
                else:
                    # Если нет активной сделки - обрабатываем сигнал немедленно
                    await queue.put(signal_data)
                    log(f"[{symbol}] Сигнал добавлен в основную очередь")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{self.symbol}] Ошибка в прослушивателе сигналов: {e}")
                await asyncio.sleep(1.0)

    async def _process_pending_signals(self, trade_key: str):
        """Обрабатывает отложенные сигналы для конкретного инструмента"""
        log = self.log or (lambda s: None)
        symbol, timeframe = trade_key.split('_', 1)
        
        log(f"[{symbol}] 🚀 Запуск обработчика отложенных сигналов для {symbol} {timeframe}")
        
        try:
            while self._running:
                # Ждем завершения активной сделки
                while trade_key in self._active_trades and self._running:
                    await asyncio.sleep(0.1)
                
                if not self._running:
                    break
                
                # Проверяем, есть ли сигналы в очереди
                if trade_key not in self._pending_signals or self._pending_signals[trade_key].empty():
                    # Если очередь пуста, выходим из цикла
                    log(f"[{symbol}] 📭 Очередь отложенных сигналов пуста, останавливаем обработчик")
                    break
                
                # Обрабатываем только последний актуальный сигнал
                last_signal = None
                try:
                    # Берем все сигналы из очереди, оставляя только последний
                    while True:
                        last_signal = self._pending_signals[trade_key].get_nowait()
                        self._pending_signals[trade_key].task_done()
                except asyncio.QueueEmpty:
                    pass
                
                if last_signal:
                    log(f"[{symbol}] 🔄 Обрабатываем отложенный сигнал для {symbol} {timeframe}")
                    
                    # Создаем задачу для обработки отложенного сигнала
                    task = asyncio.create_task(self._process_single_signal(last_signal))
                    self._active_trades[trade_key] = task
                    
                    def cleanup(fut):
                        self._active_trades.pop(trade_key, None)
                        # После завершения сделки проверяем, есть ли еще отложенные сигналы
                        asyncio.create_task(self._check_more_pending_signals(trade_key))
                    
                    task.add_done_callback(cleanup)
                    
                    # Ждем завершения обработки этого сигнала
                    await task
                
                # Небольшая пауза перед следующей проверкой
                await asyncio.sleep(0.1)
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"[{symbol}] Ошибка в обработчике отложенных сигналов: {e}")
        finally:
            # Очистка ресурсов
            if trade_key in self._pending_processing:
                del self._pending_processing[trade_key]
            if trade_key in self._pending_notified:
                self._pending_notified.discard(trade_key)
            
            log(f"[{symbol}] 🛑 Остановка обработчика отложенных сигналов для {symbol} {timeframe}")

    async def _check_more_pending_signals(self, trade_key: str):
        """Проверяет, есть ли еще отложенные сигналы после завершения сделки"""
        if trade_key in self._pending_signals:
            pending_queue = self._pending_signals[trade_key]
            if not pending_queue.empty():
                symbol, timeframe = trade_key.split('_', 1)
                log = self.log or (lambda s: None)
                log(f"[{symbol}] 📋 В отложенной очереди еще {pending_queue.qsize()} сигналов, перезапускаем обработчик")
                
                # Перезапускаем обработчик, если он не активен
                if trade_key not in self._pending_processing:
                    self._pending_processing[trade_key] = asyncio.create_task(
                        self._process_pending_signals(trade_key)
                    )
            else:
                # Если очередь пуста, очищаем уведомления
                symbol, timeframe = trade_key.split('_', 1)
                if trade_key in self._pending_notified:
                    self._pending_notified.discard(trade_key)
                    log(f"[{symbol}] ✅ Все отложенные сигналы обработаны")

    async def _signal_processor(self, queue: asyncio.Queue):
        """Обработчик сигналов из основной очереди"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск обработчика сигналов ({self.strategy_name})")
        
        while self._running:
            await self._pause_point()
            
            try:
                try:
                    signal_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                symbol = signal_data['symbol']
                timeframe = signal_data['timeframe']
                trade_key = f"{symbol}_{timeframe}"
                
                # Проверка на активные сделки (двойная проверка)
                if trade_key in self._active_trades:
                    log(f"[{symbol}] ⚠ Активная сделка появилась, перемещаем сигнал в отложенную очередь")
                    
                    # Перемещаем сигнал в отложенную очередь
                    if trade_key not in self._pending_signals:
                        self._pending_signals[trade_key] = asyncio.Queue()
                    
                    await self._pending_signals[trade_key].put(signal_data)
                    
                    # Запускаем обработчик отложенных сигналов, если нужно
                    if trade_key not in self._pending_processing and not self._pending_signals[trade_key].empty():
                        self._pending_processing[trade_key] = asyncio.create_task(
                            self._process_pending_signals(trade_key)
                        )
                    
                    queue.task_done()
                    continue
                
                # Проверка общей параллельной обработки
                if not self._allow_parallel_trades and self._active_trades:
                    log(f"[{symbol}] ⚠ Параллельная обработка запрещена, перемещаем сигнал в отложенную очередь")
                    
                    # Для простоты помещаем в очередь первого попавшегося инструмента
                    # В реальности нужно более сложное управление очередями
                    first_trade_key = next(iter(self._active_trades.keys()))
                    if first_trade_key not in self._pending_signals:
                        self._pending_signals[first_trade_key] = asyncio.Queue()
                    
                    await self._pending_signals[first_trade_key].put(signal_data)
                    queue.task_done()
                    continue
                
                # Обработка сигнала
                task = asyncio.create_task(self._process_single_signal(signal_data))
                self._active_trades[trade_key] = task
                
                def cleanup(fut):
                    self._active_trades.pop(trade_key, None)
                    queue.task_done()
                
                task.add_done_callback(cleanup)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{self.symbol}] Ошибка в обработчике сигналов: {e}")
                queue.task_done()

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Oscar Grind"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала Oscar Grind")
        
        # ОБНОВЛЯЕМ ТОЛЬКО ДОПОЛНИТЕЛЬНУЮ ИНФОРМАЦИЮ О СИГНАЛЕ
        # self._last_signal_ver и self._last_signal_at_str уже обновлены в _signal_listener
        self._last_indicator = signal_data['indicator']
        
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

        # Запускаем серию Oscar Grind для этого сигнала
        await self._run_oscar_grind_series(symbol, timeframe, direction, log)

    async def _run_oscar_grind_series(self, symbol: str, timeframe: str, initial_direction: int, log):
        """Запускает серию Oscar Grind для конкретного сигнала"""
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] 🛑 repeat_count={series_left} — нечего выполнять.")
            return

        # Параметры серии
        base_unit = float(self.params.get("base_investment", 100))
        target_profit = base_unit  # цель профита в валюте счёта
        max_steps = int(self.params.get("max_steps", 20))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        double_entry = bool(self.params.get("double_entry", True))

        if max_steps <= 0:
            log(f"[{symbol}] ⚠ max_steps={max_steps} — серию не стартуем.")
            return

        # Состояние серии Oscar Grind
        step_idx = 0
        cum_profit = 0.0  # накопленный профит серии (может уходить в минус)
        stake = base_unit  # текущая ставка (unit-кратная)

        series_started = False  # серия начинается только с первой убыточной сделки
        series_direction = initial_direction  # направление текущей ставки
        repeat_trade = False  # повторный вход после поражения

        # Основной цикл серии
        while self._running and step_idx < max_steps:
            await self._pause_point()

            if not await self.ensure_account_conditions():
                continue

            # Проверяем возраст сигнала
            max_age = self._max_signal_age_seconds()
            if max_age > 0 and self._last_signal_monotonic is not None:
                age = asyncio.get_running_loop().time() - self._last_signal_monotonic
                if age > max_age:
                    log(f"[{symbol}] ⚠ Сигнал устарел ({age:.1f}s > {max_age:.0f}s). Прерываем серию.")
                    break

            # Проверяем выплату и баланс
            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step_idx + 1} stake={format_amount(stake)} min={self._trade_minutes} "
                f"side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")

            # Определяем режим аккаунта
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # Размещаем сделку
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(
                symbol, series_direction, stake, self._anchor_ccy
            )
                    
            if not trade_id:
                log(f"[{symbol}] ❌ Не удалось разместить сделку. Прерываем серию.")
                break

            # Определяем длительность сделки
            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)

            wait_seconds = self.params.get("result_wait_s")
            if wait_seconds is None:
                wait_seconds = trade_seconds
            else:
                wait_seconds = float(wait_seconds)

            # Уведомляем о pending сделке
            self._notify_pending_trade(
                trade_id, symbol, timeframe, series_direction, stake, pct, 
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
                direction=series_direction,
                stake=float(stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
            )

            # Определим исход сделки
            if profit is None:
                log(f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                profit_val = -float(stake)
                outcome = "loss"
            else:
                profit_val = float(profit)
                if profit_val > 0.0:
                    outcome = "win"
                elif profit_val == 0.0:
                    outcome = "refund"
                else:
                    outcome = "loss"

            # До первой убыточной сделки серия не стартует
            if not series_started:
                if outcome == "loss":
                    series_started = True
                    cum_profit += profit_val
                else:
                    if outcome == "win":
                        log(f"[{symbol}] ✅ WIN до старта серии — ожидаем первую убыточную сделку.")
                    else:
                        log(f"[{symbol}] ↩️ REFUND до старта серии — ожидаем первую убыточную сделку.")
                    stake = base_unit
                    continue
            else:
                cum_profit += profit_val

            # Проверим цель
            if cum_profit >= target_profit:
                log(f"[{symbol}] ✅ Серия завершена: достигнута цель {format_amount(target_profit)} "
                    f"(накоплено {format_amount(cum_profit)}).")
                step_idx += 1
                break

            # Вычислим следующую ставку по правилам Oscar Grind
            need = max(0.0, target_profit - cum_profit)
            next_stake = self._next_stake(
                outcome=outcome,
                stake=stake,
                base_unit=base_unit,
                pct=pct,
                need=need,
                profit=0.0 if profit is None else float(profit_val),
                cum_profit=cum_profit,
                log=log,
            )

            # Переходим к следующему шагу
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

            # Обновляем время экспирации для classic
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(
                    minutes=_minutes_from_timeframe(timeframe)
                )

        if step_idx > 0:
            series_left -= 1
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

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
        log,
    ) -> float:
        """Вычисляет следующую ставку - должен быть переопределен в дочерних классах"""
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")

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
        """Остановка стратегии с очисткой отложенных сигналов"""
        # Останавливаем все обработчики отложенных сигналов
        for task in self._pending_processing.values():
            task.cancel()
        
        # Очищаем очереди
        self._pending_signals.clear()
        self._pending_processing.clear()
        self._pending_notified.clear()
        
        super().stop()
