# strategies/fixed.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
    is_demo_account,
)
from core.signal_waiter import wait_for_signal_versioned, peek_signal_state
from strategies.base import StrategyBase
from core.money import format_amount
from core.policy import normalize_sprint

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

CLASSIC_SIGNAL_MAX_AGE_SEC = 120.0
SPRINT_SIGNAL_MAX_AGE_SEC = 5.0

ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"
CLASSIC_ALLOWED_TFS = {"M5", "M15", "M30", "H1", "H4"}

DEFAULTS = {
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


def _minutes_from_timeframe(tf: str) -> int:
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


class FixedStakeStrategy(StrategyBase):
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
        **_,
    ):
        p = dict(DEFAULTS)
        if params:
            p.update(params)

        _symbol = (symbol or "").strip()
        _tf_raw = (timeframe or "").strip()
        _tf = _tf_raw.upper()
        self._use_any_symbol = _symbol == ALL_SYMBOLS_LABEL
        self._use_any_timeframe = _tf_raw == ALL_TF_LABEL

        cur_symbol = "*" if self._use_any_symbol else _symbol
        cur_tf = "*" if self._use_any_timeframe else _tf

        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=cur_symbol,
            log_callback=log_callback,
            **p,
        )

        self.http_client = http_client
        self.timeframe = cur_tf or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        raw_minutes = int(
            self.params.get("minutes", _minutes_from_timeframe(self.timeframe))
        )
        norm = normalize_sprint(cur_symbol, raw_minutes)
        if norm is None:
            fallback = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(cur_symbol, fallback) or fallback
            if self.log:
                self.log(
                    f"[{cur_symbol}] ⚠ Минуты {raw_minutes} недопустимы. Использую {norm}."
                )
        self._trade_minutes = int(norm)
        self.params["minutes"] = self._trade_minutes

        self._trade_type = str(self.params.get("trade_type", "sprint")).lower()
        self.params["trade_type"] = self._trade_type

        self._on_trade_result = self.params.get("on_trade_result")
        self._on_trade_pending = self.params.get("on_trade_pending")
        self._on_status = self.params.get("on_status")

        self._allow_parallel_trades = bool(
            self.params.get("allow_parallel_trades", True)
        )
        self.params["allow_parallel_trades"] = self._allow_parallel_trades

        self._pending_tasks: set[asyncio.Task] = set()
        self._pending_for_status: dict[str, tuple[str, str]] = {}
        
        # Новые атрибуты для параллельной обработки
        self._signal_queue: Optional[asyncio.Queue] = None
        self._signal_listener_task: Optional[asyncio.Task] = None
        self._signal_processor_task: Optional[asyncio.Task] = None
        self._active_trades: dict[str, asyncio.Task] = {}  # symbol_timeframe -> task
        self._trades_counter: int = 0  # Счетчик сделок

        def _status(msg: str):
            cb = self._on_status
            if callable(cb):
                try:
                    cb(msg)
                except Exception:
                    pass

        self._status = _status

        self._running = False
        self._last_signal_ver: Optional[int] = None
        self._last_indicator: str = "-"
        self._last_signal_at_str: Optional[str] = None
        self._next_expire_dt = None
        self._last_signal_monotonic: Optional[float] = None

        anchor = str(
            self.params.get("account_currency", DEFAULTS["account_currency"])
        ).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor

        self._anchor_is_demo: Optional[bool] = None
        self._low_payout_notified = False

    # --- управление ожиданиями ---
    def _update_pending_status(self) -> None:
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
        self._pending_for_status[str(trade_id)] = (symbol, timeframe)
        self._update_pending_status()

    def _unregister_pending_trade(self, trade_id: str) -> None:
        self._pending_for_status.pop(str(trade_id), None)
        self._update_pending_status()

    def _launch_trade_result_task(self, task: asyncio.Task) -> None:
        self._pending_tasks.add(task)

        def _cleanup(fut: asyncio.Future) -> None:
            self._pending_tasks.discard(task)

        task.add_done_callback(_cleanup)

    def stop(self):
        # Останавливаем все задачи
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
        if self._signal_processor_task:
            self._signal_processor_task.cancel()
            
        for task in list(self._pending_tasks):
            task.cancel()
        for task in list(self._active_trades.values()):
            task.cancel()
            
        self._pending_for_status.clear()
        self._active_trades.clear()
        super().stop()

    async def _wait_for_trade_result(
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
        log,
    ) -> None:
        try:
            profit = await check_trade_result(
                self.http_client,
                user_id=self.user_id,
                user_hash=self.user_hash,
                trade_id=trade_id,
                wait_time=wait_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[{symbol}] ⚠ Ошибка получения результата сделки {trade_id}: {e}")
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
                )
            except Exception:
                pass

        if profit is None:
            log(f"[{symbol}] ⚠ Результат неизвестен")
        elif profit >= 0:
            log(f"[{symbol}] ✅ Результат: {format_amount(profit)}")
        else:
            log(f"[{symbol}] ❌ Результат: {format_amount(profit)}")

        self._unregister_pending_trade(trade_id)

    async def _signal_listener(self, queue: asyncio.Queue):
        """Прослушивает сигналы и добавляет их в очередь"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск прослушивателя сигналов")
        
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
                
                await queue.put(signal_data)
                log(f"[{signal_data['symbol']}] Сигнал добавлен в очередь")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[{self.symbol}] Ошибка в прослушивателе сигналов: {e}")
                await asyncio.sleep(1.0)

    async def _signal_processor(self, queue: asyncio.Queue):
        """Обрабатывает сигналы из очереди"""
        log = self.log or (lambda s: None)
        log(f"[{self.symbol}] Запуск обработчика сигналов")
        
        while self._running:
            await self._pause_point()
            
            try:
                # Ждем сигнал из очереди с таймаутом для проверки running
                try:
                    signal_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Проверяем лимит сделок
                if self._trades_counter >= int(self.params.get("repeat_count", DEFAULTS["repeat_count"])):
                    log(f"[{signal_data['symbol']}] Достигнут лимит сделок, пропускаем сигнал")
                    queue.task_done()
                    continue
                
                # Создаем независимую задачу для обработки каждого сигнала
                trade_key = f"{signal_data['symbol']}_{signal_data['timeframe']}"
                
                # Если уже есть активная сделка для этого инструмента/таймфрейма, пропускаем
                if trade_key in self._active_trades and not self._allow_parallel_trades:
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
        """Обрабатывает один сигнал независимо от других"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала Fixed Stake")
        
        # Обновляем последнюю информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")
        
        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(MOSCOW_TZ) if ts else None

        # Обновляем символ и таймфрейм если используются "все"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe
            raw = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, raw) or raw
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

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

        min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
        if bal < min_balance:
            log(f"[{symbol}] ⛔ Баланс ниже минимума ({format_amount(bal)} < {format_amount(min_balance)}). Пропускаем сигнал.")
            return

        stake = float(self.params.get("base_investment", DEFAULTS["base_investment"]))
        min_pct = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
        wait_low = float(self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"]))
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
            
        min_floor = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
        if cur_balance is None or (cur_balance - stake) < min_floor:
            log(f"[{symbol}] 🛑 Сделка {format_amount(stake)} {account_ccy} может опустить баланс ниже "
                f"{format_amount(min_floor)} {account_ccy}"
                + ("" if cur_balance is None else f" (текущий {format_amount(cur_balance)} {account_ccy})")
                + ". Пропускаем сигнал.")
            return

        if not await self._ensure_anchor_currency():
            return
        if not await self._ensure_anchor_account_mode():
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
        trade_kwargs = {"trade_type": self._trade_type}
        time_arg = self._trade_minutes
        if self._trade_type == "classic":
            if not self._next_expire_dt:
                log(f"[{symbol}] ❌ Нет времени экспирации для classic. Пропускаем сигнал.")
                return
            time_arg = self._next_expire_dt.strftime("%H:%M")
            trade_kwargs["date"] = self._next_expire_dt.strftime("%d-%m-%Y")
            
        attempt = 0
        trade_id = None
        while attempt < 4:
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
                break
            attempt += 1
            if attempt < 4:
                log(f"[{symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                await self.sleep(1.0)
                
        if not trade_id:
            log(f"[{symbol}] ❌ Не удалось разместить сделку после 4 попыток. Пропускаем сигнал.")
            return

        # Увеличиваем счетчик сделок
        self._trades_counter += 1

        # Определяем длительность сделки
        from datetime import datetime
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - datetime.now(MOSCOW_TZ)).total_seconds(),
            )
            expected_end_ts = self._next_expire_dt.timestamp()
        else:
            trade_seconds = float(self._trade_minutes) * 60.0
            expected_end_ts = datetime.now().timestamp() + trade_seconds

        wait_seconds = self.params.get("result_wait_s")
        if wait_seconds is None:
            wait_seconds = trade_seconds
        else:
            wait_seconds = float(wait_seconds)

        # Уведомляем о pending сделке
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
                    percent=int(pct),
                    wait_seconds=float(trade_seconds),
                    account_mode=account_mode,
                    indicator=self._last_indicator,
                    expected_end_ts=expected_end_ts,
                )
            except Exception:
                pass

        self._register_pending_trade(trade_id, symbol, timeframe)

        # Ожидаем результат сделки
        ctx = dict(
            trade_id=trade_id,
            wait_seconds=float(wait_seconds),
            placed_at=placed_at_str,
            signal_at=self._last_signal_at_str,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            stake=float(stake),
            percent=int(pct),
            account_mode=account_mode,
            indicator=self._last_indicator,
            log=log,
        )

        if self._allow_parallel_trades:
            task = asyncio.create_task(self._wait_for_trade_result(**ctx))
            self._launch_trade_result_task(task)
        else:
            await self._wait_for_trade_result(**ctx)

    async def run(self) -> None:
        """Основной метод с параллельной архитектурой"""
        self._running = True
        log = self.log or (lambda s: None)

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Режим счёта: {mode_txt}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(f"[{self.symbol}] Баланс: {display} ({format_amount(amount)}), текущая валюта: {cur_ccy}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс при старте: {e}")

        # Инициализируем очередь и задачи
        self._signal_queue = asyncio.Queue()
        self._signal_listener_task = asyncio.create_task(self._signal_listener(self._signal_queue))
        self._signal_processor_task = asyncio.create_task(self._signal_processor(self._signal_queue))

        # Ждем завершения стратегии (по лимиту сделок или остановке)
        try:
            max_trades = int(self.params.get("repeat_count", DEFAULTS["repeat_count"]))
            while self._running and self._trades_counter < max_trades:
                await asyncio.sleep(1.0)
                # Обновляем статус
                remaining = max_trades - self._trades_counter
                if remaining > 0:
                    self._status(f"сделок осталось: {remaining}")
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

        # Завершаем все задачи
        if self._signal_listener_task:
            self._signal_listener_task.cancel()
        if self._signal_processor_task:
            self._signal_processor_task.cancel()

        # Ждем завершения всех активных сделок
        if self._active_trades:
            await asyncio.gather(*list(self._active_trades.values()), return_exceptions=True)
            
        if self._pending_tasks:
            await asyncio.gather(*list(self._pending_tasks), return_exceptions=True)

        self._pending_tasks.clear()
        self._active_trades.clear()
        self._pending_for_status.clear()

        log(f"[{self.symbol}] Завершение стратегии Fixed Stake. Выполнено сделок: {self._trades_counter}")

    # Остальные методы остаются без изменений...
    async def wait_signal(self, *, timeout: float) -> int:
        await self._ensure_signal_listener(self._fetch_signal_payload)

        direction, ver, meta = await self._next_signal_from_queue(timeout=timeout)
        sig_symbol = (meta or {}).get("symbol") or self.symbol
        sig_tf = ((meta or {}).get("timeframe") or self.timeframe).upper()

        self._last_signal_ver = ver
        self._last_indicator = (meta or {}).get("indicator") or "-"

        ts = (meta or {}).get("next_timestamp")
        self._next_expire_dt = ts.astimezone(MOSCOW_TZ) if ts else None

        if self._use_any_symbol:
            self.symbol = sig_symbol
        if self._use_any_timeframe:
            self.timeframe = sig_tf
            self.params["timeframe"] = self.timeframe
            raw = _minutes_from_timeframe(self.timeframe)
            norm = normalize_sprint(self.symbol, raw) or raw
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

        from datetime import datetime

        self._last_signal_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None
        return int(direction)

    def update_params(self, **params):
        super().update_params(**params)

        if "minutes" in params:
            try:
                requested = int(params["minutes"])
            except Exception:
                return
            norm = normalize_sprint(self.symbol, requested)
            if norm is None:
                if self.symbol == "BTCUSDT":
                    norm = 5 if requested < 5 else 500
                else:
                    norm = 1 if requested < 3 else max(3, min(500, requested))
                if self.log:
                    self.log(
                        f"[{self.symbol}] ⚠ Минуты {requested} недопустимы. Исправлено на {norm}."
                    )
            self._trade_minutes = int(norm)
            self.params["minutes"] = self._trade_minutes

        if "timeframe" in params:
            tf_raw = str(params["timeframe"]).strip()
            tf = tf_raw.upper()
            self._use_any_timeframe = tf_raw in (ALL_TF_LABEL, "*")
            self.timeframe = "*" if self._use_any_timeframe else tf
            self.params["timeframe"] = self.timeframe
            if "minutes" not in params:
                raw = _minutes_from_timeframe(self.timeframe)
                norm = normalize_sprint(self.symbol, raw) or raw
                self._trade_minutes = int(norm)
                self.params["minutes"] = self._trade_minutes

        if "account_currency" in params:
            want = str(params["account_currency"]).upper()
            if want != self._anchor_ccy and self.log:
                self.log(
                    f"[{self.symbol}] ⚠ Игнорирую попытку сменить валюту на лету {self._anchor_ccy} → {want}. Валюта зафиксирована при создании."
                )
            self.params["account_currency"] = self._anchor_ccy

        if "trade_type" in params:
            self._trade_type = str(params["trade_type"]).lower()
            self.params["trade_type"] = self._trade_type

    def _max_signal_age_seconds(self) -> float:
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

    async def _fetch_signal_payload(
        self, since_version: Optional[int]
    ) -> tuple[int, int, dict[str, Optional[str | int | float]]]:
        grace = float(self.params.get("grace_delay_sec", DEFAULTS["grace_delay_sec"]))

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

            direction, ver, meta = await self.wait_cancellable(coro, timeout=None)
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
