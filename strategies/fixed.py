from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ, ALL_SYMBOLS_LABEL, ALL_TF_LABEL, CLASSIC_ALLOWED_TFS
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_balance_info, get_current_percent, place_trade, check_trade_result

FIXED_DEFAULTS = {
    "base_investment": 100,
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


class FixedStakeStrategy(BaseTradingStrategy):
    """Стратегия с фиксированной ставкой"""
    
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
        
        # Специфичные атрибуты для Fixed Stake
        self._trades_counter: int = 0  # Счетчик сделок

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для фиксированной ставки"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала (Fixed Stake)")
        
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

        # Проверяем лимит сделок
        max_trades = int(self.params.get("repeat_count", 10))
        if self._trades_counter >= max_trades:
            log(f"[{symbol}] 🛑 Достигнут лимит сделок ({self._trades_counter}/{max_trades}). Пропускаем сигнал.")
            return

        # Запускаем обработку сделки с фиксированной ставкой
        await self._process_fixed_trade(symbol, timeframe, direction, log)

    async def _process_fixed_trade(self, symbol: str, timeframe: str, direction: int, log):
        """Обрабатывает одну сделку с фиксированной ставкой"""
        # Проверяем баланс
        try:
            bal, _, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            bal = 0.0

        min_balance = float(self.params.get("min_balance", 100))
        if bal < min_balance:
            log(f"[{symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Пропускаем сигнал.")
            return

        stake = float(self.params.get("base_investment", 100))
        min_pct = int(self.params.get("min_percent", 70))
        wait_low = float(self.params.get("wait_on_low_percent", 1))
        account_ccy = self._anchor_ccy

        # Проверяем возраст сигнала
        max_age = self._max_signal_age_seconds()
        if max_age > 0 and self._last_signal_monotonic is not None:
            age = asyncio.get_running_loop().time() - self._last_signal_monotonic
            if age > max_age:
                log(f"[{symbol}] ⚠ Сигнал устарел ({age:.1f}s > {max_age:.0f}s). Пропускаем.")
                return

        # Получаем payout
        pct = await get_current_percent(
            self.http_client,
            investment=stake,
            option=symbol,
            minutes=self._trade_minutes,
            account_ccy=account_ccy,
            trade_type=self._trade_type,
        )
        
        if pct is None:
            self._status("ожидание процента")
            log(f"[{symbol}] ⚠ Не получили % выплаты. Пропускаем сигнал.")
            return
            
        if pct < min_pct:
            self._status("ожидание высокого процента")
            if not self._low_payout_notified:
                log(f"[{symbol}] ℹ Низкий payout {pct}% < {min_pct}% — пропускаем сигнал.")
                self._low_payout_notified = True
            return
            
        if self._low_payout_notified:
            log(f"[{symbol}] ℹ Работа продолжается (текущий payout = {pct}%)")
            self._low_payout_notified = False

        # Проверяем баланс для конкретной сделки
        try:
            cur_balance, _, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            cur_balance = None
            
        min_floor = float(self.params.get("min_balance", 100))
        if cur_balance is None or (cur_balance - stake) < min_floor:
            log(f"[{symbol}] 🛑 Сделка {format_amount(stake)} {account_ccy} может опустить баланс ниже "
                f"{format_amount(min_floor)} {account_ccy}"
                + ("" if cur_balance is None else f" (текущий {format_amount(cur_balance)} {account_ccy})")
                + ". Пропускаем сигнал.")
            return

        if not await self.ensure_account_conditions():
            return

        log(f"[{symbol}] stake={format_amount(stake)} min={self._trade_minutes} "
            f"side={'UP' if direction == 1 else 'DOWN'} payout={pct}%")

        try:
            demo_now = await is_demo_account(self.http_client)
        except Exception:
            demo_now = False
        account_mode = "ДЕМО" if demo_now else "РЕАЛ"

        # Размещаем сделку
        self._status("делает ставку")
        trade_id = await self.place_trade_with_retry(
            symbol, direction, stake, self._anchor_ccy
        )
                
        if not trade_id:
            log(f"[{symbol}] ❌ Не удалось разместить сделку. Пропускаем сигнал.")
            return

        # Увеличиваем счетчик сделок
        self._trades_counter += 1

        # Определяем длительность сделки
        trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)

        wait_seconds = self.params.get("result_wait_s")
        if wait_seconds is None:
            wait_seconds = trade_seconds
        else:
            wait_seconds = float(wait_seconds)

        # Уведомляем о pending сделке
        self._notify_pending_trade(
            trade_id, symbol, timeframe, direction, stake, pct, 
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
            direction=direction,
            stake=float(stake),
            percent=int(pct),
            account_mode=account_mode,
            indicator=self._last_indicator,
        )

        # Логируем результат
        if profit is None:
            log(f"[{symbol}] ⚠ Результат неизвестен")
        elif profit >= 0:
            log(f"[{symbol}] ✅ Результат: {format_amount(profit)}")
        else:
            log(f"[{symbol}] ❌ Результат: {format_amount(profit)}")

        # Обновляем статус с оставшимися сделками
        max_trades = int(self.params.get("repeat_count", 10))
        remaining = max_trades - self._trades_counter
        if remaining > 0:
            self._status(f"сделок осталось: {remaining}")
        else:
            self._status("достигнут лимит сделок")

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

    async def run(self) -> None:
        """Запуск стратегии с отслеживанием лимита сделок"""
        self._running = True
        log = self.log or (lambda s: None)

        # Инициализация аккаунта через базовый класс
        await self._initialize_account()

        # Запуск обработки сигналов
        signal_queue = asyncio.Queue()
        self._signal_listener_task = asyncio.create_task(self._signal_listener(signal_queue))
        self._signal_processor_task = asyncio.create_task(self._signal_processor(signal_queue))

        # Основной цикл с отслеживанием лимита сделок
        try:
            max_trades = int(self.params.get("repeat_count", 10))
            while self._running and self._trades_counter < max_trades:
                await asyncio.sleep(1.0)
                # Обновляем статус
                remaining = max_trades - self._trades_counter
                if remaining > 0:
                    self._status(f"сделок осталось: {remaining}")
                else:
                    self._status("достигнут лимит сделок")
                    break
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

        log(f"[{self.symbol}] Завершение стратегии Fixed Stake. Выполнено сделок: {self._trades_counter}")

    async def _signal_processor(self, queue: asyncio.Queue):
        """Обработчик сигналов с проверкой лимита сделок"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск обработчика сигналов (Fixed Stake)")
        
        while self._running:
            await self._pause_point()
            
            try:
                try:
                    signal_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Проверяем лимит сделок
                max_trades = int(self.params.get("repeat_count", 10))
                if self._trades_counter >= max_trades:
                    log(f"[{signal_data['symbol']}] 🛑 Достигнут лимит сделок ({self._trades_counter}/{max_trades}). Пропускаем сигнал.")
                    queue.task_done()
                    continue
                
                # Проверка параллельной обработки
                if not self._allow_parallel_trades and self._active_trades:
                    log(f"[{signal_data['symbol']}] ⚠ Пропускаем сигнал (параллельная обработка запрещена)")
                    queue.task_done()
                    continue
                
                trade_key = f"{signal_data['symbol']}_{signal_data['timeframe']}"
                
                if trade_key in self._active_trades:
                    log(f"[{signal_data['symbol']}] Активная сделка уже существует, пропускаем сигнал")
                    queue.task_done()
                    continue
                
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

    def stop(self):
        """Остановка стратегии с дополнительной логикой"""
        super().stop()
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Fixed Stake остановлена. Выполнено сделок: {self._trades_counter}")

    def update_params(self, **params):
        """Обновление параметров с дополнительной логикой"""
        super().update_params(**params)
        
        # Можно добавить специфичную логику для Fixed Stake при обновлении параметров
        if "repeat_count" in params:
            max_trades = int(params["repeat_count"])
            remaining = max_trades - self._trades_counter
            if remaining > 0:
                self._status(f"сделок осталось: {remaining}")
            else:
                self._status("достигнут лимит сделок")
