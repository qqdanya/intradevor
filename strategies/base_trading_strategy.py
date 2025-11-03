from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Callable, Any
from zoneinfo import ZoneInfo

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
    is_demo_account,
)
from core.signal_waiter import wait_for_signal_versioned
from core.money import format_amount
from core.policy import normalize_sprint

from strategies.base import StrategyBase

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

CLASSIC_SIGNAL_MAX_AGE_SEC = 120.0
SPRINT_SIGNAL_MAX_AGE_SEC = 5.0

ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"
CLASSIC_ALLOWED_TFS = {"M5", "M15", "M30", "H1", "H4"}

TRADING_DEFAULTS = {
    "base_investment": 100,
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


def _minutes_from_timeframe(tf: str) -> int:
    """Конвертация таймфрейма в минуты"""
    if not tf:
        return 1
    unit = tf[0].upper()
    try:
        n = int(tf[1:])
    except Exception:
        return 1
    if unit == "M":
        return n
    if unit == "H":
        return n * 60
    if unit == "D":
        return n * 60 * 24
    if unit == "W":
        return n * 60 * 24 * 7
    return 1


class BaseTradingStrategy(StrategyBase):
    """
    Базовый класс для торговых стратегий, объединяющий управление жизненным циклом
    из StrategyBase и торговую логику.
    """
    
    def __init__(
        self,
        http_client: HttpClient,
        user_id: str,
        user_hash: str,
        symbol: str,
        log_callback=None,
        *,
        timeframe: str = "M1",
        params: Optional[dict] = None,
        strategy_name: str = "BaseTrading",
        **kwargs
    ):
        # Объединяем параметры по умолчанию
        trading_params = dict(TRADING_DEFAULTS)
        if params:
            trading_params.update(params)

        _symbol = (symbol or "").strip()
        _tf_raw = (timeframe or "").strip()
        _tf = _tf_raw.upper()
        self._use_any_symbol = _symbol == ALL_SYMBOLS_LABEL
        self._use_any_timeframe = _tf_raw == ALL_TF_LABEL

        cur_symbol = "*" if self._use_any_symbol else _symbol
        cur_tf = "*" if self._use_any_timeframe else _tf

        # Инициализация базового класса
        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=cur_symbol,
            log_callback=log_callback,
            **trading_params,
            **kwargs
        )

        self.http_client = http_client
        self.timeframe = cur_tf or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe
        self.strategy_name = strategy_name

        # Инициализация торговых параметров
        self._init_trading_params()

        # Колбэки
        self._on_trade_result = self.params.get("on_trade_result")
        self._on_trade_pending = self.params.get("on_trade_pending")
        self._on_status = self.params.get("on_status")

        # Состояние стратегии
        self._last_signal_ver: Optional[int] = None
        self._last_indicator: str = "-"
        self._last_signal_at_str: Optional[str] = None
        self._next_expire_dt = None
        self._last_signal_monotonic: Optional[float] = None

        # Параллельная обработка
        self._allow_parallel_trades = bool(self.params.get("allow_parallel_trades", True))
        self.params["allow_parallel_trades"] = self._allow_parallel_trades

        # Активные сделки и задачи
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_for_status: dict[str, tuple[str, str]] = {}
        self._active_trades: dict[str, asyncio.Task] = {}

        # Аккаунт
        anchor = str(self.params.get("account_currency", "RUB")).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor
        self._anchor_is_demo: Optional[bool] = None
        self._low_payout_notified = False

    def _init_trading_params(self):
        """Инициализация торговых параметров"""
        raw_minutes = int(self.params.get("minutes", _minutes_from_timeframe(self.timeframe)))
        norm = normalize_sprint(self.symbol, raw_minutes)
        if norm is None:
            fallback = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, fallback) or fallback
            if self.log:
                self.log(f"[{self.symbol}] ⚠ Минуты {raw_minutes} недопустимы. Использую {norm}.")
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

        self._trade_type = str(self.params.get("trade_type", "sprint")).lower()
        self.params["trade_type"] = self._trade_type

    # === STATUS MANAGEMENT ===

    def _status(self, msg: str):
        """Обновление статуса стратегии"""
        self._emit_status(msg)

    def _update_pending_status(self) -> None:
        """Обновление статуса ожидающих сделок"""
        if not self._pending_for_status:
            self._status("ожидание сигнала")
            return

        parts = []
        for symbol, timeframe in self._pending_for_status.values():
            sym = str(symbol or "-")
            tf = str(timeframe or "-")
            parts.append(f"{sym} {tf}")
        if not parts:
            self._status("ожидание сигнала")
            return

        shown = parts[:3]
        extra = len(parts) - len(shown)
        text = ", ".join(shown)
        if extra > 0:
            text += f" +{extra}"
        self._status(f"ожидание результата: {text}")

    def _register_pending_trade(self, trade_id: str, symbol: str, timeframe: str) -> None:
        """Регистрация ожидающей сделки"""
        self._pending_for_status[str(trade_id)] = (symbol, timeframe)
        self._update_pending_status()

    def _unregister_pending_trade(self, trade_id: str) -> None:
        """Удаление ожидающей сделки"""
        self._pending_for_status.pop(str(trade_id), None)
        self._update_pending_status()

    # === TRADING METHODS ===

    async def place_trade_with_retry(
        self, 
        symbol: str, 
        direction: int, 
        stake: float, 
        account_ccy: str,
        max_attempts: int = 4
    ) -> Optional[str]:
        """Размещение сделки с повторными попытками"""
        log = self.log or (lambda s: None)
        
        trade_kwargs = {"trade_type": self._trade_type}
        time_arg = self._trade_minutes
        if self._trade_type == "classic":
            if not self._next_expire_dt:
                log(f"[{symbol}] ❌ Нет времени экспирации для classic.")
                return None
            time_arg = self._next_expire_dt.strftime("%H:%M")
            trade_kwargs["date"] = self._next_expire_dt.strftime("%d-%m-%Y")
            
        for attempt in range(max_attempts):
            trade_id = await place_trade(
                self.http_client,
                user_id=self.user_id,
                user_hash=self.user_hash,
                investment=stake,
                option=symbol,
                status=direction,
                minutes=time_arg,
                account_ccy=account_ccy,
                strict=True,
                on_log=log,
                **trade_kwargs,
            )
            if trade_id:
                return trade_id
            if attempt < max_attempts - 1:
                log(f"[{symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                await self.sleep(1.0)
                    
        return None

    async def wait_for_trade_result(
        self,
        *,
        trade_id: str,
        wait_seconds: float,
        placed_at: str,
        signal_at: Optional[str],
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        account_mode: Optional[str],
        indicator: str,
    ) -> Optional[float]:
        """Ожидание результата сделки"""
        self._status("ожидание результата")

        try:
            profit = await check_trade_result(
                self.http_client,
                user_id=self.user_id,
                user_hash=self.user_hash,
                trade_id=trade_id,
                wait_time=wait_seconds,
            )
        except Exception:
            profit = None

        # Вызов колбэка результата
        if callable(self._on_trade_result):
            try:
                self._on_trade_result(
                    trade_id=trade_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_at=signal_at,
                    placed_at=placed_at,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    profit=(None if profit is None else float(profit)),
                    account_mode=account_mode,
                    indicator=indicator,
                )
            except Exception:
                pass

        self._unregister_pending_trade(trade_id)
        return None if profit is None else float(profit)

    async def check_payout_and_balance(
        self, 
        symbol: str, 
        stake: float, 
        min_pct: int,
        wait_low: float
    ) -> tuple[Optional[int], Optional[float]]:
        """Проверка выплаты и баланса"""
        account_ccy = self._anchor_ccy
        
        # Проверка выплаты
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
            return None, None
            
        if pct < min_pct:
            self._status("ожидание высокого процента")
            if not self._low_payout_notified:
                (self.log or (lambda s: None))(f"[{symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём...")
                self._low_payout_notified = True
            await self.sleep(wait_low)
            return None, None
            
        if self._low_payout_notified:
            (self.log or (lambda s: None))(f"[{symbol}] ℹ Работа продолжается (текущий payout = {pct}%)")
            self._low_payout_notified = False

        # Проверка баланса
        try:
            cur_balance, _, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            cur_balance = None
            
        min_floor = float(self.params.get("min_balance", 100))
        if cur_balance is None or (cur_balance - stake) < min_floor:
            (self.log or (lambda s: None))(
                f"[{symbol}] 🛑 Сделка {format_amount(stake)} {account_ccy} может опустить баланс ниже "
                f"{format_amount(min_floor)} {account_ccy}"
                + ("" if cur_balance is None else f" (текущий {format_amount(cur_balance)} {account_ccy})")
            )
            return None, None

        return pct, cur_balance

    async def ensure_account_conditions(self) -> bool:
        """Проверка условий аккаунта"""
        if not await self._ensure_anchor_currency():
            return False
        if not await self._ensure_anchor_account_mode():
            return False
        return True

    async def _ensure_anchor_currency(self) -> bool:
        """Проверка валюты аккаунта"""
        try:
            _, ccy_now, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
        except Exception:
            ccy_now = None
        if ccy_now != self._anchor_ccy:
            self._status(f"ожидание смены валюты на {self._anchor_ccy}")
            await self.sleep(1.0)
            return False
        return True

    async def _ensure_anchor_account_mode(self) -> bool:
        """Проверка режима аккаунта"""
        try:
            demo_now = await is_demo_account(self.http_client)
        except Exception:
            self._status("ожидание проверки режима счёта")
            await self.sleep(1.0)
            return False

        if self._anchor_is_demo is None:
            self._anchor_is_demo = bool(demo_now)

        if bool(demo_now) != bool(self._anchor_is_demo):
            need = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            self._status(f"ожидание смены счёта на {need}")
            await self.sleep(1.0)
            return False
        return True

    # === SIGNAL PROCESSING ===

    async def _signal_listener(self, queue: asyncio.Queue):
        """Прослушиватель сигналов"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск прослушивателя сигналов ({self.strategy_name})")
        
        _parallel_block_notified = False
        
        while self._running:
            await self._pause_point()
            
            # Проверяем блокировку параллельной обработки
            if not self._allow_parallel_trades and self._active_trades:
                if not _parallel_block_notified:
                    log(f"[{self.symbol}] ⏳ Ожидание завершения активных сделок перед приемом новых сигналов")
                    _parallel_block_notified = True
                await asyncio.sleep(0.5)
                continue
            elif _parallel_block_notified:
                log(f"[{self.symbol}] ✅ Возобновление приема сигналов")
                _parallel_block_notified = False
                
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
                
                await queue.put(signal_data)
                log(f"[{signal_data['symbol']}] Сигнал добавлен в очередь")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{self.symbol}] Ошибка в прослушивателе сигналов: {e}")
                await asyncio.sleep(1.0)

    async def _signal_processor(self, queue: asyncio.Queue):
        """Обработчик сигналов"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск обработчика сигналов ({self.strategy_name})")
        
        while self._running:
            await self._pause_point()
            
            try:
                try:
                    signal_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
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

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала (должен быть реализован в дочерних классах)"""
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")

    async def _fetch_signal_payload(
        self, since_version: Optional[int]
    ) -> tuple[int, int, dict[str, Optional[str | int | float]]]:
        """Получение сигнала"""
        grace = float(self.params.get("grace_delay_sec", 30.0))

        def _on_delay(sec: float):
            (self.log or (lambda s: None))(
                f"[{self.symbol}] ⏱ Задержка следующего прогноза ~{sec:.1f}s"
            )

        listen_symbol = "*" if self._use_any_symbol else self.symbol
        listen_timeframe = "*" if self._use_any_timeframe else self.timeframe
        current_version = since_version

        while True:
            coro = wait_for_signal_versioned(
                listen_symbol,
                listen_timeframe,
                since_version=current_version,
                check_pause=self.is_paused,
                timeout=None,
                raise_on_timeout=True,
                grace_delay_sec=grace,
                on_delay=_on_delay,
                include_meta=True,
                max_age_sec=self._max_signal_age_seconds(),
            )

            direction, ver, meta = await asyncio.wait_for(coro, timeout=None)
            current_version = ver

            sig_symbol = (meta or {}).get("symbol") or listen_symbol
            sig_tf = ((meta or {}).get("timeframe") or listen_timeframe).upper()

            if (
                self._use_any_timeframe
                and self._trade_type == "classic"
                and sig_tf not in CLASSIC_ALLOWED_TFS
            ):
                if self.log:
                    self.log(
                        f"[{sig_symbol}] ⚠ Таймфрейм {sig_tf} недоступен для Classic — пропуск."
                    )
                continue

            return int(direction), int(ver), meta

    def _max_signal_age_seconds(self) -> float:
        """Максимальный возраст сигнала"""
        base = 0.0
        if self._trade_type == "classic":
            base = CLASSIC_SIGNAL_MAX_AGE_SEC
        elif self._trade_type == "sprint":
            base = SPRINT_SIGNAL_MAX_AGE_SEC

        if not self._allow_parallel_trades:
            return base

        wait_window = float(self.params.get("result_wait_s") or 0.0)
        if wait_window <= 0.0:
            wait_window = float(self._trade_minutes) * 60.0
        else:
            wait_window = max(wait_window, float(self._trade_minutes) * 60.0)

        return max(base, wait_window + 5.0)

    # === STRATEGY MANAGEMENT ===

    async def run(self) -> None:
        """Запуск стратегии"""
        self._running = True
        log = self.log or (lambda s: None)

        # Инициализация аккаунта
        await self._initialize_account()

        # Запуск обработки сигналов
        signal_queue = asyncio.Queue()
        self._signal_listener_task = asyncio.create_task(self._signal_listener(signal_queue))
        self._signal_processor_task = asyncio.create_task(self._signal_processor(signal_queue))

        # Основной цикл
        try:
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _initialize_account(self):
        """Инициализация аккаунта"""
        log = self.log or (lambda s: None)

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Режим счёта: {mode_txt} ({self.strategy_name})")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(f"[{self.symbol}] Баланс: {display} ({format_amount(amount)}), валюта: {cur_ccy}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс: {e}")

    async def _shutdown(self):
        """Завершение работы стратегии"""
        self._running = False

        # Отмена задач
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
        if self._signal_processor_task:
            self._signal_processor_task.cancel()

        # Ожидание завершения активных сделок
        if self._active_trades:
            await asyncio.gather(*list(self._active_trades.values()), return_exceptions=True)
            
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

        # Очистка
        self._pending_tasks.clear()
        self._active_trades.clear()
        self._pending_for_status.clear()

        (self.log or (lambda s: None))(f"[{self.symbol}] Завершение стратегии {self.strategy_name}")

    def stop(self):
        """Остановка стратегии"""
        super().stop()
        self._pending_for_status.clear()
        self._active_trades.clear()

    # === PARAMETER UPDATES ===

    def update_params(self, **params):
        """Обновление параметров"""
        super().update_params(**params)
        
        if "minutes" in params:
            self._update_minutes_param(params["minutes"])
            
        if "timeframe" in params:
            self._update_timeframe_param(params["timeframe"])
            
        if "account_currency" in params:
            self._update_currency_param(params["account_currency"])
            
        if "trade_type" in params:
            self._trade_type = str(params["trade_type"]).lower()
            self.params["trade_type"] = self._trade_type

        if "allow_parallel_trades" in params:
            self._allow_parallel_trades = bool(params["allow_parallel_trades"])
            self.params["allow_parallel_trades"] = self._allow_parallel_trades

    def _update_minutes_param(self, minutes):
        """Обновление параметра минут"""
        try:
            requested = int(minutes)
        except Exception:
            return
        norm = normalize_sprint(self.symbol, requested)
        if norm is None:
            if self.symbol == "BTCUSDT":
                norm = 5 if requested < 5 else 500
            else:
                norm = 1 if requested < 3 else max(3, min(500, requested))
            if self.log:
                self.log(f"[{self.symbol}] ⚠ Минуты {requested} недопустимы. Исправлено на {norm}.")
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

    def _update_timeframe_param(self, timeframe):
        """Обновление параметра таймфрейма"""
        tf_raw = str(timeframe).strip()
        tf = tf_raw.upper()
        self._use_any_timeframe = tf_raw in (ALL_TF_LABEL, "*")
        self.timeframe = "*" if self._use_any_timeframe else tf
        self.params["timeframe"] = self.timeframe
        if "minutes" not in self.params:
            raw = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, raw) or raw
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

    def _update_currency_param(self, currency):
        """Обновление параметра валюты"""
        want = str(currency).upper()
        if want != self._anchor_ccy and self.log:
            self.log(f"[{self.symbol}] ⚠ Игнорирую попытку сменить валюту на лету {self._anchor_ccy} → {want}.")
        self.params["account_currency"] = self._anchor_ccy
