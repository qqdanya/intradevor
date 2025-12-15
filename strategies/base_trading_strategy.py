from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable, Any

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    check_trade_result,
    place_trade,
    is_demo_account,
)
from core.payout_provider import get_cached_payout
from core.signal_waiter import wait_for_signal_versioned
from core.money import format_amount
from core.policy import normalize_sprint
from core.trade_queue import result_queue, trade_queue

from strategies.base import StrategyBase
from strategies.strategy_common import StrategyCommon
from strategies.constants import *  # noqa: F403, F401
from strategies.strategy_helpers import MOSCOW_ZONE, refresh_signal_context
from strategies.timeframe_utils import minutes_from_timeframe
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
        **kwargs,
    ):
        # Объединяем параметры по умолчанию
        trading_params = dict(DEFAULTS)  # noqa: F405
        if params:
            trading_params.update(params)

        _symbol = (symbol or "").strip()
        _tf_raw = (timeframe or "").strip()
        _tf = _tf_raw.upper()

        self._use_any_symbol = _symbol == ALL_SYMBOLS_LABEL  # noqa: F405
        self._use_any_timeframe = _tf_raw == ALL_TF_LABEL  # noqa: F405

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
            **kwargs,
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
        self._next_expire_dt: Optional[datetime] = None
        self._last_signal_monotonic: Optional[float] = None

        # Счетчики серий
        self._series_counters: dict[str, int] = {}

        # Параллельная обработка
        self._allow_parallel_trades = bool(self.params.get("allow_parallel_trades", True))
        self.params["allow_parallel_trades"] = self._allow_parallel_trades

        # Активные сделки и задачи
        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_for_status: dict[str, tuple[str, str]] = {}
        self._active_trades: dict[str, asyncio.Task] = {}

        # Отложенная остановка
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

        # Планируемые ставки (для UI)
        self._planned_stakes: dict[str, float] = {}

    # =========================================================================
    # TIME / ZONE HELPERS (унификация)
    # =========================================================================

    def now_moscow(self) -> datetime:
        return datetime.now(MOSCOW_ZONE)

    def trade_duration(self) -> tuple[float, float]:
        """
        Единый расчёт длительности сделки:
        - classic: до next_expire_dt
        - иначе: minutes*60
        Возвращает (trade_seconds, expected_end_ts)
        """
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - self.now_moscow()).total_seconds(),
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds
        return trade_seconds, expected_end_ts

    def notify_pending_trade(
        self,
        *,
        trade_id: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        trade_seconds: float,
        account_mode: str,
        expected_end_ts: float,
        signal_at: Optional[str] = None,
        series_label: Optional[str] = None,
        step_label: Optional[str] = None,
    ) -> None:
        """
        Единый pending-notify. Стратегии больше не должны дублировать это.
        """
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        trade_key = self.build_trade_key(symbol, timeframe)

        if series_label is None:
            series_label = self.format_series_label(trade_key)

        self._set_planned_stake(trade_key, stake)

        if callable(self._on_trade_pending):
            try:
                self._on_trade_pending(
                    trade_id=trade_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    signal_at=signal_at or self._last_signal_at_str,
                    placed_at=placed_at_str,
                    direction=direction,
                    stake=float(stake),
                    percent=int(percent),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                    series=series_label,
                    step=step_label,
                )
            except Exception:
                pass

    async def wait_while_payout_low_and_maybe_refresh_signal(
        self,
        *,
        trade_key: str,
        symbol: str,
        poll_s: float | None = None,
    ) -> tuple[str, Optional[dict]]:
        """
        Унифицированный цикл ожидания восстановления payout ДО старта серии/сделки.
        Пока ждём — можем подхватить самый свежий сигнал из StrategyCommon.
        Возвращает: (symbol, newer_signal_or_none)
        """
        if poll_s is None:
            poll_s = float(self.params.get("wait_on_low_percent", 1))

        from strategies.strategy_helpers import is_payout_low_now  # локально, чтобы избежать циклов

        while self._running:
            await self._pause_point()

            low = await is_payout_low_now(self, symbol)
            if not low:
                return symbol, None

            common = getattr(self, "_common", None)
            if common is not None:
                newer = common.pop_latest_signal(trade_key)
                if newer:
                    return (newer.get("symbol") or symbol), newer

            await asyncio.sleep(float(poll_s))

        return symbol, None

    # =========================================================================
    # UI HELPERS
    # =========================================================================

    def build_trade_key(self, symbol: str, timeframe: str) -> str:
        """Формирует ключ серии/сделок с учётом настройки общей серии."""
        base_symbol = (symbol or "*").strip()
        base_timeframe = (timeframe or "*").strip()

        if self.params.get("use_common_series"):
            return f"common_{self.strategy_name}"

        return f"{base_symbol}_{base_timeframe}"

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        """Формирует строку вида 'Текущая/Всего' для серии."""
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

    def format_step_label(self, step_idx: int | None, max_steps: int | None) -> str | None:
        """Возвращает строку 'Текущий/Максимум' для шага серии."""
        try:
            cur = int(step_idx) if step_idx is not None else None
            total = int(max_steps) if max_steps is not None else None
        except Exception:
            return None

        if cur is None or total is None or total <= 0 or cur < 0:
            return None

        return f"{min(cur + 1, total)}/{total}"

    def get_planned_stake(self, trade_key: str) -> float | None:
        return self._planned_stakes.get(trade_key)

    def _set_planned_stake(self, trade_key: str, stake: float) -> None:
        try:
            self._planned_stakes[trade_key] = float(stake)
        except Exception:
            pass

    # =========================================================================
    # SERIES COUNTERS
    # =========================================================================

    def _get_series_left(self, trade_key: str) -> int:
        max_series = int(self.params.get("repeat_count", 10))
        remaining = self._series_counters.get(trade_key)
        if remaining is None:
            remaining = max_series
        else:
            remaining = max(0, min(int(remaining), max_series))
        self._series_counters[trade_key] = remaining
        return remaining

    def _set_series_left(self, trade_key: str, value: int) -> int:
        max_series = int(self.params.get("repeat_count", 10))
        clamped = max(0, min(int(value), max_series))
        self._series_counters[trade_key] = clamped
        self._check_all_series_completed(self._series_counters)
        return clamped

    def _reset_series_counter(self, trade_key: str) -> None:
        self._series_counters.pop(trade_key, None)

    def _check_all_series_completed(self, series_map: dict[str, int]) -> None:
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

    # =========================================================================
    # INIT / PARAMS
    # =========================================================================

    def _init_trading_params(self) -> None:
        self._auto_minutes = bool(self.params.get("auto_minutes", False))
        self.params["auto_minutes"] = self._auto_minutes

        raw_minutes = int(self.params.get("minutes", minutes_from_timeframe(self.timeframe)))
        if self._auto_minutes and str(self.params.get("trade_type", "sprint")).lower() == "sprint":
            raw_minutes = minutes_from_timeframe(self.timeframe)

        self._apply_minutes(raw_minutes)
        self._trade_type = str(self.params.get("trade_type", "sprint")).lower()
        self.params["trade_type"] = self._trade_type

    def should_request_fresh_signal_after_loss(self) -> bool:
        return False

    # =========================================================================
    # SIGNAL VALIDATION
    # =========================================================================

    def _is_signal_valid_for_classic(
        self,
        signal_data: dict,
        current_time: datetime,
        for_placement: bool = True,
    ) -> tuple[bool, str]:
        next_expire = signal_data.get("next_expire")
        if not next_expire:
            return False, "нет next_timestamp"

        if for_placement:
            time_until_next = (next_expire - current_time).total_seconds()
            if time_until_next <= 0:
                return False, f"следующая свеча уже наступила в {next_expire.strftime('%H:%M:%S')}"

            min_required_time = float(self.params.get("classic_min_time_before_next_sec", 180.0)) + float(
                self.params.get("classic_trade_buffer_sec", 10.0)
            )
            if time_until_next < min_required_time:
                return False, f"до следующей свечи осталось {time_until_next:.0f}с < {min_required_time:.0f}с"

        signal_timestamp = signal_data["timestamp"]
        signal_age = (current_time - signal_timestamp).total_seconds()
        max_signal_age = float(self.params.get("classic_signal_max_age_sec", 170.0))

        if signal_age > max_signal_age:
            return False, f"сигналу {signal_age:.0f}с > {max_signal_age:.0f}с"

        return True, "актуален"

    def _is_signal_valid_for_sprint(self, signal_data: dict, current_time: datetime) -> tuple[bool, str]:
        signal_timestamp = signal_data["timestamp"]
        signal_age = (current_time - signal_timestamp).total_seconds()
        max_signal_age = SPRINT_SIGNAL_MAX_AGE_SEC  # noqa: F405
        if signal_age > max_signal_age:
            return False, f"сигналу {signal_age:.1f}с > {max_signal_age}с"
        return True, "актуален"

    # =========================================================================
    # STATUS
    # =========================================================================

    def _status(self, msg: str) -> None:
        self._emit_status(msg)

    def _update_pending_status(self) -> None:
        if not self._pending_for_status:
            self._status("ожидание сигнала")
            return
        parts: list[str] = []
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
        self._pending_for_status[str(trade_id)] = (symbol, timeframe)
        self._update_pending_status()

    def _unregister_pending_trade(self, trade_id: str) -> None:
        self._pending_for_status.pop(str(trade_id), None)
        self._update_pending_status()
        self._fulfill_stop_request_if_idle()

    def _request_stop_when_idle(self, reason: Optional[str] = None) -> None:
        if reason is not None:
            self._stop_when_idle_reason = reason
        if not self._stop_when_idle_requested:
            self._stop_when_idle_requested = True
        if not self._pending_for_status:
            self._fulfill_stop_request_if_idle()

    def _fulfill_stop_request_if_idle(self) -> None:
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

    # =========================================================================
    # TRADING
    # =========================================================================

    async def place_trade_with_retry(
        self,
        symbol: str,
        direction: int,
        stake: float,
        account_ccy: str,
        max_attempts: int = 4,
    ) -> Optional[str]:
        log = self.log or (lambda s: None)

        trade_kwargs: dict[str, Any] = {"trade_type": self._trade_type}
        time_arg: Any = self._trade_minutes

        if self._trade_type == "classic":
            if not self._next_expire_dt:
                log(classic_expire_missing(symbol))
                return None
            time_arg = self._next_expire_dt.strftime("%H:%M")
            trade_kwargs["date"] = self._next_expire_dt.strftime("%d-%m-%Y")

        for attempt in range(max_attempts):
            trade_id = await trade_queue.enqueue(
                lambda: place_trade(
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
        step_label: str | None = None,
    ) -> Optional[float]:
        self._status("ожидание результата")
        try:
            profit = await result_queue.enqueue(
                lambda: check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=wait_seconds,
                )
            )
        except Exception:
            profit = None

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
                    step=step_label,
                )
            except Exception:
                pass

        trade_key = self.build_trade_key(symbol, timeframe)
        self._planned_stakes.pop(trade_key, None)

        self._unregister_pending_trade(trade_id)
        return None if profit is None else float(profit)

    async def check_payout_and_balance(
        self,
        symbol: str,
        stake: float,
        min_pct: int,
        wait_low: float,
    ) -> tuple[Optional[int], Optional[float]]:
        account_ccy = self._anchor_ccy

        pct = await get_cached_payout(
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
                (self.log or (lambda s: None))(
                    f"[{symbol}] ℹ Низкий payout {pct}% < {min_pct}% — ждём..."
                )
                self._low_payout_notified = True
            await self.sleep(wait_low)
            return None, None

        if self._low_payout_notified:
            (self.log or (lambda s: None))(
                f"[{symbol}] ℹ Работа продолжается (текущий payout = {pct}%)"
            )
            self._low_payout_notified = False

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
                + (
                    ""
                    if cur_balance is None
                    else f" (текущий {format_amount(cur_balance)} {account_ccy})"
                )
            )
            return None, None

        return int(pct), cur_balance

    async def ensure_account_conditions(self) -> bool:
        if not await self._ensure_anchor_currency():
            return False
        if not await self._ensure_anchor_account_mode():
            return False
        return True

    async def _ensure_anchor_currency(self) -> bool:
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

    # =========================================================================
    # SIGNAL PROCESSING
    # =========================================================================

    async def _signal_listener(self, queue: asyncio.Queue):
        await self._common.signal_listener(queue)

    async def _process_single_signal(self, signal_data: dict):
        raise NotImplementedError("Метод должен быть реализован в дочернем классе")

    # =========================================================================
    # SERIALIZATION HELPERS
    # =========================================================================

    def is_series_active(self, trade_key: str) -> bool:
        return False

    def allow_concurrent_trades_per_key(self) -> bool:
        return False

    async def _fetch_signal_payload(
        self, since_version: Optional[int]
    ) -> tuple[int, int, dict[str, Optional[str | int | float]]]:
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
                and sig_tf not in CLASSIC_ALLOWED_TFS  # noqa: F405
            ):
                if self.log:
                    self.log(classic_timeframe_unavailable(sig_symbol, sig_tf))
                continue

            return int(direction), int(ver), meta

    def _max_signal_age_seconds(self) -> float:
        base = 0.0
        if self._trade_type == "classic":
            base = CLASSIC_SIGNAL_MAX_AGE_SEC  # noqa: F405
        elif self._trade_type == "sprint":
            return SPRINT_SIGNAL_MAX_AGE_SEC  # noqa: F405

        if not self._allow_parallel_trades:
            return base

        wait_window = float(self.params.get("result_wait_s") or 0.0)
        if wait_window <= 0.0:
            wait_window = float(self._trade_minutes) * 60.0
        else:
            wait_window = max(wait_window, float(self._trade_minutes) * 60.0)

        return max(base, wait_window + 5.0)

    # =========================================================================
    # STRATEGY MANAGEMENT
    # =========================================================================

    async def run(self) -> None:
        self._running = True
        self._status("ожидание сигнала")

        await self._initialize_account()

        signal_queue = asyncio.Queue()
        self._signal_listener_task = asyncio.create_task(self._signal_listener(signal_queue))

        try:
            while self._running:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _initialize_account(self) -> None:
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

    async def _shutdown(self) -> None:
        self._running = False

        if self._signal_listener_task:
            self._signal_listener_task.cancel()

        if self._active_trades:
            await asyncio.gather(*list(self._active_trades.values()), return_exceptions=True)

        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

        self._pending_tasks.clear()
        self._active_trades.clear()
        self._pending_for_status.clear()

        (self.log or (lambda s: None))(strategy_shutdown(self.symbol, self.strategy_name))

    def stop(self) -> None:
        self._stop_when_idle_requested = False
        self._stop_when_idle_reason = None
        self._common.stop()
        super().stop()
        self._pending_for_status.clear()
        self._active_trades.clear()
        self._series_counters.clear()

    # =========================================================================
    # PARAMETER UPDATES
    # =========================================================================

    def update_params(self, **params) -> None:
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

        if "auto_minutes" in params:
            self._auto_minutes = bool(params["auto_minutes"])
            self.params["auto_minutes"] = self._auto_minutes
            if self._auto_minutes and self._trade_type == "sprint":
                self._maybe_set_auto_minutes(self.timeframe)

        if "allow_parallel_trades" in params:
            self._allow_parallel_trades = bool(params["allow_parallel_trades"])
            self.params["allow_parallel_trades"] = self._allow_parallel_trades

        if "use_common_series" in params:
            self.params["use_common_series"] = bool(params["use_common_series"])

        if "repeat_count" in params:
            try:
                max_series = int(params["repeat_count"])
            except Exception:
                max_series = int(self.params.get("repeat_count", 10))

            for key in list(self._series_counters.keys()):
                self._series_counters[key] = max(0, min(self._series_counters[key], max_series))

    def _apply_minutes(self, requested: int, allow_correction: bool = False) -> None:
        norm = normalize_sprint(self.symbol, requested)
        if norm is None:
            fallback = minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, fallback) or fallback
            if self.log and allow_correction:
                self.log(minutes_invalid(self.symbol, requested, norm, corrected=True))
            elif self.log:
                self.log(minutes_invalid(self.symbol, requested, norm))
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

    def _maybe_set_auto_minutes(self, timeframe: str) -> None:
        if not (self._auto_minutes and self._trade_type == "sprint"):
            return
        raw = minutes_from_timeframe(timeframe)
        self._apply_minutes(raw)

    def _update_minutes_param(self, minutes) -> None:
        try:
            requested = int(minutes)
        except Exception:
            return
        self._apply_minutes(requested, allow_correction=True)

    def _update_timeframe_param(self, timeframe) -> None:
        tf_raw = str(timeframe).strip()
        tf = tf_raw.upper()
        self._use_any_timeframe = tf_raw in (ALL_TF_LABEL, "*")  # noqa: F405
        self.timeframe = "*" if self._use_any_timeframe else tf
        self.params["timeframe"] = self.timeframe
        if self._auto_minutes and self._trade_type == "sprint":
            self._maybe_set_auto_minutes(self.timeframe)
        elif "minutes" not in self.params:
            raw = minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, raw) or raw
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

    def _update_currency_param(self, currency) -> None:
        want = str(currency).upper()
        if want != self._anchor_ccy and self.log:
            self.log(currency_change_ignored(self.symbol, self._anchor_ccy, want))
        self.params["account_currency"] = self._anchor_ccy
