from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.money import format_amount
from core.intrade_api_async import (
    is_demo_account,
)
from core.payout_provider import get_cached_payout
from core.time_utils import format_local_time
from strategies.log_messages import (
    start_processing,
    signal_not_actual,
    signal_not_actual_for_placement,
    trade_placement_failed,
    trade_summary,
    result_unknown,
    result_win,
    result_loss,
    trade_limit_reached,
    fixed_stake_stopped,
    payout_missing,
    payout_too_low,
    payout_resumed,
    trade_timeout,  # используется в _wait_for_new_signal (как в Fibonacci)
)

FIXED_DEFAULTS = {
    "base_investment": 100,
    "repeat_count": 10,  # лимит сделок (по trade_key) в новой архитектуре
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    # Для фиксированной ставки ждём результат всю длительность сделки (classic/sprint)
    "result_wait_s": None,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class FixedStakeStrategy(BaseTradingStrategy):
    """
    Fixed Stake (1 сделка на сигнал), адаптировано под новую архитектуру:
    - repeat_count ведём через BaseTradingStrategy._get_series_left/_set_series_left (по trade_key)
    - при LOW payout — коротко ждём и подхватываем самый свежий сигнал из StrategyCommon
    - при устаревании перед размещением — ждём новый сигнал, серию/лимит НЕ "съедаем"
    """

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

        # Используем как "флаг занятости" только если allow_parallel_trades=False
        self._active_trade: dict[str, bool] = {}

        # Счётчик успешно размещённых сделок (общий, для stop-лога)
        self._placed_trades_total: int = 0

        # Флаг уведомления о низком payout (для логов payout_* ниже)
        self._low_payout_notified: bool = False

    # =====================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # =====================================================================

    def allow_concurrent_trades_per_key(self) -> bool:
        """Совместимость со старым поведением: можно параллелить (если allow_parallel_trades=True)."""
        return bool(self.params.get("allow_parallel_trades", True))

    def is_series_active(self, trade_key: str) -> bool:
        """Для FixedStake серия как таковая отсутствует, но при запрете параллели блокируем trade_key."""
        if self.allow_concurrent_trades_per_key():
            return False
        return self._active_trade.get(trade_key, False)

    def _calc_next_candle_from_now(self, timeframe: str) -> datetime:
        """Для classic: вернуть время начала СЛЕДУЮЩЕЙ свечи относительно текущего момента."""
        now = datetime.now(ZoneInfo(MOSCOW_TZ))
        tf_minutes = _minutes_from_timeframe(timeframe)

        base = now.replace(second=0, microsecond=0)
        total_min = base.hour * 60 + base.minute
        next_total = (total_min // tf_minutes + 1) * tf_minutes

        days_add = next_total // (24 * 60)
        minutes_in_day = next_total % (24 * 60)
        hour = minutes_in_day // 60
        minute = minutes_in_day % 60

        return (base + timedelta(days=days_add)).replace(hour=hour, minute=minute)

    async def _is_payout_low_now(self, symbol: str, stake: float) -> tuple[bool, Optional[int]]:
        """
        Проверка payout прямо сейчас (короткая) + статус/логи.
        Возвращает (is_low, pct_or_None).
        """
        min_pct = int(self.params.get("min_percent", 70))
        account_ccy = self._anchor_ccy

        try:
            pct = await get_cached_payout(
                self.http_client,
                investment=float(stake),
                option=symbol,
                minutes=self._trade_minutes,
                account_ccy=account_ccy,
                trade_type=self._trade_type,
            )
        except Exception:
            pct = None

        if pct is None:
            self._status("ожидание процента")
            log = self.log or (lambda s: None)
            log(payout_missing(symbol))
            return True, None

        if int(pct) < min_pct:
            self._status("ожидание высокого процента")
            log = self.log or (lambda s: None)
            if not self._low_payout_notified:
                log(payout_too_low(symbol, int(pct), min_pct))
                self._low_payout_notified = True
            return True, int(pct)

        if self._low_payout_notified:
            log = self.log or (lambda s: None)
            log(payout_resumed(symbol, int(pct)))
            self._low_payout_notified = False

        return False, int(pct)

    async def _wait_for_new_signal(
        self,
        trade_key: str,
        log,
        symbol: str,
        timeframe: str,
        timeout: float = 30.0,
    ) -> Optional[dict]:
        """Ожидает новый сигнал в течение timeout секунд (как в Fibonacci)."""
        start_time = asyncio.get_event_loop().time()

        while self._running and (asyncio.get_event_loop().time() - start_time) < timeout:
            await self._pause_point()

            if hasattr(self, "_common") and self._common is not None:
                new_signal = self._common.pop_latest_signal(trade_key)
                if new_signal:
                    return new_signal

            await asyncio.sleep(0.5)

        log(trade_timeout(symbol, timeout))
        return None

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки (classic по next_expire_dt, иначе minutes)."""
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

    def format_series_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        """
        Для FixedStake показываем счётчик сделок: текущая/максимум (по trade_key).
        series_left — остаток ДО списания текущей сделки.
        """
        return "1/1"

    def format_step_label(self, trade_key: str, *, series_left: int | None = None) -> str | None:
        """Возвращает номер текущей ставки в формате "Текущая/Максимум"."""

        try:
            total = int(self.params.get("repeat_count", 0))
        except Exception:
            total = 0
        if total <= 0:
            return None

        if series_left is None:
            series_left = self._get_series_left(trade_key)

        try:
            remaining = int(series_left)
        except Exception:
            remaining = total

        current = max(1, min(total, total - remaining + 1))
        return f"{current}/{total}"

    # =====================================================================
    # ОСНОВНАЯ ЛОГИКА
    # =====================================================================

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Fixed Stake (1 сделка)."""
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = int(signal_data["direction"])

        self._maybe_set_auto_minutes(timeframe)
        trade_key = self.build_trade_key(symbol, timeframe)

        log = self.log or (lambda s: None)

        # Если параллель запрещена — паркуем сигнал в StrategyCommon, как в Fibonacci
        if not self.allow_concurrent_trades_per_key() and self._active_trade.get(trade_key):
            if hasattr(self, "_common") and self._common is not None:
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        # Сколько сделок ещё можно сделать по этому trade_key
        series_left = self._get_series_left(trade_key)
        if series_left <= 0:
            if not self._stop_when_idle_requested:
                done = int(self.params.get("repeat_count", 0))
                log(trade_limit_reached(symbol, done, done))
                self._status("достигнут лимит сделок")
            self._request_stop_when_idle("достигнут лимит сделок")
            return

        # Обновляем "*"
        if self._use_any_symbol:
            self.symbol = symbol
        if self._use_any_timeframe:
            self.timeframe = timeframe
            self.params["timeframe"] = self.timeframe

        # Обновляем инфо о сигнале
        self._last_signal_ver = signal_data.get("version")
        self._last_indicator = signal_data.get("indicator")
        self._last_signal_at_str = format_local_time(signal_data["timestamp"])

        ts = signal_data.get("meta", {}).get("next_timestamp") if signal_data.get("meta") else None
        self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

        try:
            self._last_signal_monotonic = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_signal_monotonic = None

        log(start_processing(symbol, "Fixed Stake"))

        # Валидация сигнала (до любых ожиданий)
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
            if not ok:
                log(signal_not_actual(symbol, "classic", reason))
                return
        else:
            ok, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
            if not ok:
                log(signal_not_actual(symbol, "sprint", reason))
                return

        # LOW PAYOUT: коротко ждём и подхватываем самый свежий сигнал
        stake = float(self.params.get("base_investment", 100))
        wait_low = float(self.params.get("wait_on_low_percent", 1))

        while self._running:
            await self._pause_point()
            is_low, _pct = await self._is_payout_low_now(symbol, stake)
            if not is_low:
                break

            # пока ждём — берём свежий сигнал (если есть)
            if hasattr(self, "_common") and self._common is not None:
                newer = self._common.pop_latest_signal(trade_key)
                if newer:
                    signal_data = newer
                    symbol = signal_data["symbol"]
                    timeframe = signal_data["timeframe"]
                    direction = int(signal_data["direction"])
                    self._maybe_set_auto_minutes(timeframe)
                    trade_key = self.build_trade_key(symbol, timeframe)

                    # обновляем контекст сигнала
                    self._last_signal_ver = signal_data.get("version", self._last_signal_ver)
                    self._last_indicator = signal_data.get("indicator", self._last_indicator)
                    self._last_signal_at_str = format_local_time(signal_data["timestamp"])

                    ts = signal_data.get("meta", {}).get("next_timestamp") if signal_data.get("meta") else None
                    self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

            await asyncio.sleep(wait_low)

        # Финальная проверка актуальности перед размещением:
        # если устарел — ждём новый сигнал, лимит НЕ уменьшаем
        current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
        if self._trade_type == "classic":
            ok, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
        else:
            sprint_payload = signal_data if signal_data.get("timestamp") else {"timestamp": signal_data["timestamp"]}
            ok, reason = self._is_signal_valid_for_sprint(sprint_payload, current_time)

        if not ok:
            log(signal_not_actual_for_placement(symbol, reason))
            if hasattr(self, "_common") and self._common is not None:
                timeout = float(self.params.get("signal_timeout_sec", 30.0))
                new_signal = await self._wait_for_new_signal(trade_key, log, symbol, timeframe, timeout=timeout)
                if not new_signal:
                    return
                # Переподхватили новый сигнал — перезапускаем обработку “как будто это текущий”
                await self._process_single_signal(new_signal)
            return

        # classic: экспирация = следующая свеча от "сейчас"
        if self._trade_type == "classic":
            self._next_expire_dt = self._calc_next_candle_from_now(timeframe)

        # summary
        # payout мы уже проверили выше через cached payout, берём ещё раз (дешево) чтобы в логах было актуально
        _, pct_now = await self._is_payout_low_now(symbol, stake)
        pct_now = int(pct_now) if pct_now is not None else 0

        log(
            trade_summary(
                symbol,
                format_amount(stake),
                self._trade_minutes,
                direction,
                pct_now,
            )
        )

        # lock trade_key если параллель запрещена
        locked = False
        if not self.allow_concurrent_trades_per_key():
            self._active_trade[trade_key] = True
            locked = True

        try:
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # Размещаем сделку
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, direction, stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return  # лимит НЕ уменьшаем

            self._placed_trades_total += 1

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds_cfg = self.params.get("result_wait_s")
            wait_seconds = (
                trade_seconds
                if wait_seconds_cfg is None or float(wait_seconds_cfg) <= 0
                else float(wait_seconds_cfg)
            )

            signal_at_str = signal_data.get("signal_time_str") or format_local_time(signal_data["timestamp"])
            series_label = self.format_series_label(trade_key, series_left=series_left)
            step_label = self.format_step_label(trade_key, series_left=series_left)

            # pending notify
            self._set_planned_stake(trade_key, stake)
            if callable(self._on_trade_pending):
                try:
                    self._on_trade_pending(
                        trade_id=trade_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        signal_at=signal_at_str,
                        placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                        direction=direction,
                        stake=float(stake),
                        percent=int(pct_now),
                        wait_seconds=float(trade_seconds),
                        account_mode=account_mode,
                        indicator=self._last_indicator,
                        expected_end_ts=expected_end_ts,
                        series=series_label,
                        step=step_label,
                    )
                except Exception:
                    pass

            self._register_pending_trade(trade_id, symbol, timeframe)

            # Ждём результат
            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=float(stake),
                percent=int(pct_now),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
                step_label=step_label,
            )

            if profit is None:
                log(result_unknown(symbol))
            elif profit >= 0:
                log(result_win(symbol, f"Результат: {format_amount(profit)}"))
            else:
                log(result_loss(symbol, f"Результат: {format_amount(profit)}"))

            # Лимит сделок уменьшаем ТОЛЬКО если сделка была размещена
            series_left = max(0, int(series_left) - 1)
            self._set_series_left(trade_key, series_left)

            if series_left > 0:
                self._status(f"сделок осталось: {series_left}")
            else:
                self._status("достигнут лимит сделок")
                self._request_stop_when_idle("достигнут лимит сделок")

        finally:
            if locked:
                self._active_trade.pop(trade_key, None)

    def stop(self):
        log = self.log or (lambda s: None)
        log(fixed_stake_stopped(self.symbol, self._placed_trades_total))
        super().stop()

    def update_params(self, **params):
        super().update_params(**params)
        # В новой архитектуре “остаток” считается по trade_key через series_left,
        # поэтому здесь ничего не пересчитываем глобально.
