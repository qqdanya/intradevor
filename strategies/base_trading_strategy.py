from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
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
from strategies.strategy_common import StrategyCommon
from strategies.constants import *  # Импортируем все константы
from strategies.log_messages import (
    account_mode,
    account_mode_error,
    balance_error,
    balance_info,
    classic_expire_missing,
    classic_timeframe_unavailable,
    currency_change_ignored,
    minutes_invalid,
    strategy_shutdown,
    trade_retry,
)

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
        trading_params = dict(DEFAULTS)  # Используем DEFAULTS из constants
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
        self._last_signal_ver: int = 0
        self._last_indicator: str = "-"
        self._last_signal_at_str: Optional[str] = None
        self._next_expire_dt = None
        self._last_signal_monotonic: Optional[float] = None

        # Счетчики серий для стратегий (кроме Мартингейла, у которого своя реализация)
        self._series_counters: dict[str, int] = {}
        
        # Параллельная обработка
        self._allow_parallel_trades = bool(self.params.get("allow_parallel_trades", True))
        self.params["allow_parallel_trades"] = self._allow_parallel_trades
        
        # Активные сделки и задачи
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_for_status: dict[str, tuple[str, str]] = {}
        self._active_trades: dict[str, asyncio.Task] = {}

        # Отложенная остановка (ожидание завершения сделок)
        self._stop_when_idle_requested: bool = False
        self._stop_when_idle_reason: Optional[str] = None

        # Аккаунт
        anchor = str(self.params.get("account_currency", "RUB")).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor
        self._anchor_is_demo: Optional[bool] = None
        self._low_payout_notified = False
        
        # Общая логика обработки сигналов
        self._common = StrategyCommon(self)

        # Планируемые ставки по ключу сделки (для отображения в UI)
        self._planned_stakes: dict[str, float] = {}

    # === UI HELPERS ===
    def format_series_label(
        self, trade_key: str, *, series_left: int | None = None
    ) -> str | None:
        """Формирует строку вида "Текущая/Всего" для отображения серии."""

        try:
            total = int(self.params.get("repeat_count", 0))
        except Exception:
            total = 0

        if total <= 0:
            return None

        if series_left is None:
            remaining = self._series_counters.get(trade_key)
        else:
            remaining = series_left

        try:
            remaining_int = int(remaining) if remaining is not None else total
        except Exception:
            remaining_int = total

        remaining_int = max(0, min(remaining_int, total))
        current = max(1, min(total, total - remaining_int + 1))
        return f"{current}/{total}"

    def get_planned_stake(self, trade_key: str) -> float | None:
        """Возвращает последнюю рассчитанную ставку для ключа сделки."""

        return self._planned_stakes.get(trade_key)

    def _set_planned_stake(self, trade_key: str, stake: float) -> None:
        """Сохраняет ставку для дальнейшего отображения в очередях."""

        try:
            self._planned_stakes[trade_key] = float(stake)
        except Exception:
            pass

    # === SERIES COUNTERS ===
    def _get_series_left(self, trade_key: str) -> int:
        """Возвращает оставшееся количество серий для ключа сделки."""
        max_series = int(self.params.get("repeat_count", 10))
        remaining = self._series_counters.get(trade_key)

        if remaining is None:
            remaining = max_series
        else:
            remaining = max(0, min(int(remaining), max_series))

        self._series_counters[trade_key] = remaining
        return remaining

    def _set_series_left(self, trade_key: str, value: int) -> int:
        """Обновляет количество оставшихся серий для ключа сделки."""
        max_series = int(self.params.get("repeat_count", 10))
        clamped = max(0, min(int(value), max_series))
        self._series_counters[trade_key] = clamped
        self._check_all_series_completed(self._series_counters)
        return clamped

    def _reset_series_counter(self, trade_key: str) -> None:
        """Сбрасывает счетчик серий для указанного ключа сделки."""
        self._series_counters.pop(trade_key, None)

    def _check_all_series_completed(self, series_map: dict[str, int]) -> None:
        """Останавливает стратегию, если все серии для всех пар и ТФ завершены."""

        if not (self._use_any_symbol and self._use_any_timeframe):
            return

        if not series_map:
            return

        try:
            has_remaining = any(int(v) > 0 for v in series_map.values())
        except Exception:
            has_remaining = True

        if has_remaining:
            return

        self._request_stop_when_idle("все серии завершены для всех валютных пар и таймфреймов")

    def _init_trading_params(self):
        """Инициализация торговых параметров"""
        raw_minutes = int(self.params.get("minutes", _minutes_from_timeframe(self.timeframe)))
        norm = normalize_sprint(self.symbol, raw_minutes)
        if norm is None:
            fallback = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, fallback) or fallback
            if self.log:
                self.log(minutes_invalid(self.symbol, raw_minutes, norm))
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes
        self._trade_type = str(self.params.get("trade_type", "sprint")).lower()
        self.params["trade_type"] = self._trade_type

    def should_request_fresh_signal_after_loss(self) -> bool:
        """Возвращает True, если стратегии нужен новый сигнал после убыточной сделки."""
        return False

    # === SIGNAL VALIDATION METHODS ===
    def _is_signal_valid_for_classic(self, signal_data: dict, current_time: datetime, for_placement: bool = True) -> tuple[bool, str]:
        """
        Проверяет актуальность сигнала для classic-торгов
        for_placement: True - проверка перед размещением ставки, False - проверка в процессе серии
        """
        next_expire = signal_data.get('next_expire')
        if not next_expire:
            return False, "нет next_timestamp"
        
        # Если проверка для РАЗМЕЩЕНИЯ ставки - проверяем время до следующей свечи
        if for_placement:
            time_until_next = (next_expire - current_time).total_seconds()
            
            # Если следующая свеча уже наступила - нельзя размещать новую ставку
            if time_until_next <= 0:
                return False, f"следующая свеча уже наступила в {next_expire.strftime('%H:%M:%S')}"
            
            # Проверяем что до следующей свечи осталось достаточно времени для размещения
            min_required_time = self.params.get("classic_min_time_before_next_sec", 180.0) + \
                               self.params.get("classic_trade_buffer_sec", 10.0)
            
            if time_until_next < min_required_time:
                return False, f"до следующей свечи осталось {time_until_next:.0f}с < {min_required_time:.0f}с"
        
        # Проверка возраста сигнала (всегда актуальна)
        signal_timestamp = signal_data['timestamp']
        signal_age = (current_time - signal_timestamp).total_seconds()
        max_signal_age = self.params.get("classic_signal_max_age_sec", 170.0)
        
        if signal_age > max_signal_age:
            return False, f"сигналу {signal_age:.0f}с > {max_signal_age:.0f}с"
        
        return True, "актуален"
        
    def _is_signal_valid_for_sprint(self, signal_data: dict, current_time: datetime) -> tuple[bool, str]:
        """Проверяет актуальность сигнала для sprint-торгов"""
        signal_timestamp = signal_data['timestamp']
        signal_age = (current_time - signal_timestamp).total_seconds()
        
        # 🔴 МЕНЯЕМ: максимальный возраст 5 секунд вместо 55
        max_signal_age = 5.0  # Всего 5 секунд!
        
        if signal_age > max_signal_age:
            return False, f"сигналу {signal_age:.1f}с > {max_signal_age}с"
        
        return True, "актуален"

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
        self._fulfill_stop_request_if_idle()

    def _request_stop_when_idle(self, reason: Optional[str] = None) -> None:
        """Планирует остановку стратегии после завершения активных сделок."""
        if reason is not None:
            self._stop_when_idle_reason = reason
        if not self._stop_when_idle_requested:
            self._stop_when_idle_requested = True
        if not self._pending_for_status:
            self._fulfill_stop_request_if_idle()

    def _fulfill_stop_request_if_idle(self) -> None:
        """Останавливает стратегию, если запрошена остановка и нет активных сделок."""
        if not self._stop_when_idle_requested:
            return
        if self._pending_for_status:
            return

        reason = self._stop_when_idle_reason
        self._stop_when_idle_requested = False
        self._stop_when_idle_reason = None

        if reason:
            self._status(reason)

        if not self.is_stopped():
            self.stop()

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
                log(classic_expire_missing(symbol))
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
                log(trade_retry(symbol))
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
        series_label: str | None = None,
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
                    series=series_label,
                )
            except Exception:
                pass

        trade_key = f"{symbol}_{timeframe}"
        # После завершения сделки планируемая ставка может измениться
        self._planned_stakes.pop(trade_key, None)

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
        """Прослушиватель сигналов - использует общую логику"""
        await self._common.signal_listener(queue)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала (должен быть реализован в дочерних классах)"""
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")

    # === SERIALIZATION HELPERS ===
    def is_series_active(self, trade_key: str) -> bool:
        """Возвращает True, если для указанного ключа уже выполняется серия."""
        # По умолчанию стратегия не ограничивает параллельность по ключу
        return False

    def allow_concurrent_trades_per_key(self) -> bool:
        """Разрешает открывать несколько сделок для одного ключа одновременно."""
        return False

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
                    self.log(classic_timeframe_unavailable(sig_symbol, sig_tf))
                continue
            return int(direction), int(ver), meta

    def _max_signal_age_seconds(self) -> float:
        """Максимальный возраст сигнала. Для sprint — жёсткий лимит 5.0s."""
        # базовые значения (взятые из констант)
        base = 0.0
        if self._trade_type == "classic":
            base = CLASSIC_SIGNAL_MAX_AGE_SEC
        elif self._trade_type == "sprint":
            # Жёстко ограничиваем sprint до 5 секунд — чтобы сигналы старше 5с
            # не попадали в слушатель и не создавали спам-логи.
            return 5.0

        # если разрешены параллельные сделки — расширяем окно ожидания
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
            log(account_mode(self.symbol, mode_txt, self.strategy_name))
        except Exception as e:
            log(account_mode_error(self.symbol, e))
            self._anchor_is_demo = False
        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(balance_info(self.symbol, display, format_amount(amount), cur_ccy))
        except Exception as e:
            log(balance_error(self.symbol, e))

    async def _shutdown(self):
        """Завершение работы стратегии"""
        self._running = False
        
        # Отмена задач
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
            
        # Ожидание завершения активных сделок
        if self._active_trades:
            await asyncio.gather(*list(self._active_trades.values()), return_exceptions=True)
           
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)
            
        # Очистка
        self._pending_tasks.clear()
        self._active_trades.clear()
        self._pending_for_status.clear()
        (self.log or (lambda s: None))(strategy_shutdown(self.symbol, self.strategy_name))

    def stop(self):
        """Остановка стратегии"""
        self._stop_when_idle_requested = False
        self._stop_when_idle_reason = None
        self._common.stop()
        super().stop()
        self._pending_for_status.clear()
        self._active_trades.clear()
        self._series_counters.clear()

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

        if "repeat_count" in params:
            try:
                max_series = int(params["repeat_count"])
            except Exception:
                max_series = int(self.params.get("repeat_count", 10))

            for key in list(self._series_counters.keys()):
                self._series_counters[key] = max(0, min(self._series_counters[key], max_series))

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
                self.log(minutes_invalid(self.symbol, requested, norm, corrected=True))
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
            self.log(currency_change_ignored(self.symbol, self._anchor_ccy, want))
        self.params["account_currency"] = self._anchor_ccy
