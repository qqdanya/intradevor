from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.time_utils import format_local_time
from core.money import format_amount
from core.intrade_api_async import is_demo_account, get_current_percent
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    result_unknown,
    series_completed,
    trade_step,
    win_with_parlay,
    push_repeat_same_stake,
    loss_series_finish,
    steps_limit_reached,
    series_remaining,
    trade_result_removed,
)

ANTIMARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 3,               # по умолчанию 3 шага
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 300,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}


class AntiMartingaleStrategy(BaseTradingStrategy):
    """Антимартингейл с очередями и параллельной обработкой.
    Отличия:
      - Увеличиваем ставку ПОСЛЕ WIN на размер фактического выигрыша (парлей).
      - Серию продолжаем только после WIN; при LOSS серия завершается.
      - Из очереди сигналов (StrategyCommon) удаляем после WIN или PUSH (refund).
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
        anti_params = dict(ANTIMARTINGALE_DEFAULTS)
        if params:
            anti_params.update(params)

        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=anti_params,
            strategy_name="AntiMartingale",
            **kwargs,
        )

        # Активные серии по ключу сделки (symbol+timeframe)
        self._active_series: dict[str, bool] = {}
        self._series_remaining: dict[str, int] = {}

        # Очередь сигналов на время низкого payout по ключу сделки
        # trade_key -> list[signal_data]
        self._low_payout_queues: dict[str, list[dict]] = {}

    # =====================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # =====================================================================

    def _calc_next_candle_from_now(self, timeframe: str) -> datetime:
        """
        Для classic: вернуть время начала СЛЕДУЮЩЕЙ свечи относительно текущего момента.
        Привязка к размеру таймфрейма (M1, M5, M15 и т.д.).
        """
        now = datetime.now(ZoneInfo(MOSCOW_TZ))
        tf_minutes = _minutes_from_timeframe(timeframe)

        base = now.replace(second=0, microsecond=0)

        total_min = base.hour * 60 + base.minute
        next_total = (total_min // tf_minutes + 1) * tf_minutes  # ближайший следующий слот

        days_add = next_total // (24 * 60)
        minutes_in_day = next_total % (24 * 60)
        hour = minutes_in_day // 60
        minute = minutes_in_day % 60

        next_dt = (base + timedelta(days=days_add)).replace(hour=hour, minute=minute)
        return next_dt

    async def _is_payout_low_now(self, symbol: str) -> bool:
        """
        Проверка, низкий ли payout прямо сейчас (без запуска серии).
        Используем базовый размер ставки и текущие торговые минуты.
        """
        min_pct = int(self.params.get("min_percent", 70))
        stake = float(self.params.get("base_investment", 100))
        account_ccy = self._anchor_ccy

        try:
            pct = await get_current_percent(
                self.http_client,
                investment=stake,
                option=symbol,
                minutes=self._trade_minutes,
                account_ccy=account_ccy,
                trade_type=self._trade_type,
            )
        except Exception:
            pct = None

        if pct is None:
            self._status("ожидание процента")
            return True

        if pct < min_pct:
            self._status("ожидание высокого процента")
            return True

        return False

    def _enqueue_low_payout_signal(self, trade_key: str, signal_data: dict) -> None:
        """Кладём сигнал в очередь, пока payout низкий (до старта серии)."""
        self._low_payout_queues.setdefault(trade_key, []).append(signal_data)

    def _pop_latest_from_low_payout_queue(self, trade_key: str) -> Optional[dict]:
        """
        Берём самый свежий сигнал из очереди для данного trade_key.
        После этого очередь очищается.
        """
        queue = self._low_payout_queues.get(trade_key)
        if not queue:
            return None
        latest = queue[-1]
        self._low_payout_queues[trade_key] = []
        return latest

    def _is_sprint_signal_valid_for_series(
        self,
        signal_data: dict,
        now_dt: datetime,
        *,
        consecutive_non_win: int,
    ) -> tuple[bool, str]:
        """
        Специальная проверка актуальности sprint-сигнала для серийной торговли (Антимартин).

        Базовый sprint в BaseTradingStrategy ограничен 5 секундами — этого мало
        для повторных сделок по одному сигналу внутри серии.

        Здесь окно растёт как:
            window = (1 + consecutive_non_win) * trade_duration

        где trade_duration = self._trade_minutes * 60 (секунд).
        """
        raw_ts = signal_data.get("timestamp")
        if raw_ts is None:
            return False, "нет timestamp у сигнала"

        if raw_ts.tzinfo is None:
            signal_ts = raw_ts.replace(tzinfo=ZoneInfo(MOSCOW_TZ))
        else:
            signal_ts = raw_ts.astimezone(ZoneInfo(MOSCOW_TZ))

        trade_sec = float(self._trade_minutes) * 60.0
        candles = max(1, 1 + consecutive_non_win)
        max_age = candles * trade_sec

        age = (now_dt - signal_ts).total_seconds()
        if age > max_age:
            return False, f"сигналу {age:.1f}с > {max_age:.0f}с"

        return True, "актуален"

    # =====================================================================
    # ПУБЛИЧНЫЕ МЕТОДЫ
    # =====================================================================

    def is_series_active(self, trade_key: str) -> bool:
        """Проверка, выполняется ли серия для указанного ключа."""
        return self._active_series.get(trade_key, False)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Антимартингейла"""
        symbol = signal_data["symbol"]
        timeframe = signal_data["timeframe"]
        direction = signal_data["direction"]

        self._maybe_set_auto_minutes(timeframe)

        log = self.log or (lambda s: None)

        trade_key = self.build_trade_key(symbol, timeframe)

        # 1) Если уже есть активная серия по этому ключу — отдаём сигнал в StrategyCommon
        if trade_key in self._active_series and self._active_series[trade_key]:
            log(series_already_active(symbol, timeframe))
            if hasattr(self, "_common"):
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

        # 2) Если НЕТ активной серии — сначала проверяем payout.
        #    Если payout низкий — кладём сигнал в свою очередь и выходим.
        if await self._is_payout_low_now(symbol):
            self._enqueue_low_payout_signal(trade_key, signal_data)
            return

        # 3) Payout нормальный. Если есть очередь "низкого payout" —
        #    добавляем туда текущий сигнал и берём самый свежий.
        queued_signal = self._pop_latest_from_low_payout_queue(trade_key)
        if queued_signal is not None:
            self._enqueue_low_payout_signal(trade_key, signal_data)
            queued_signal = self._pop_latest_from_low_payout_queue(trade_key)
            if queued_signal is not None:
                signal_data = queued_signal
                symbol = signal_data["symbol"]
                timeframe = signal_data["timeframe"]
                direction = signal_data["direction"]
                self._maybe_set_auto_minutes(timeframe)

        # 4) К этому моменту:
        #    - нет активной серии
        #    - payout нормальный
        #    - signal_data — самый свежий актуальный сигнал

        max_series = int(self.params.get("repeat_count", 10))
        remaining_series = self._series_remaining.get(trade_key)
        if remaining_series is None:
            remaining_series = max_series
            self._series_remaining[trade_key] = remaining_series
        if remaining_series <= 0:
            log(repeat_count_empty(symbol, remaining_series))
            return

        series_started = False
        try:
            # Метаданные сигнала
            self._last_signal_ver = signal_data["version"]
            self._last_indicator = signal_data["indicator"]
            self._last_signal_at_str = format_local_time(signal_data["timestamp"])

            ts = signal_data.get("meta", {}).get("next_timestamp")
            self._next_expire_dt = ts.astimezone(ZoneInfo(MOSCOW_TZ)) if ts else None

            if self._use_any_symbol:
                self.symbol = symbol
            if self._use_any_timeframe:
                self.timeframe = timeframe
                self.params["timeframe"] = self.timeframe

            try:
                self._last_signal_monotonic = asyncio.get_running_loop().time()
            except RuntimeError:
                self._last_signal_monotonic = None

            # Проверка актуальности сигнала перед стартом серии
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(
                    signal_data,
                    current_time,
                    for_placement=True,
                )
                if not is_valid:
                    log(signal_not_actual(symbol, "classic", reason))
                    return
            else:
                # Для sprint при запуске серии — строгие 5 секунд
                is_valid, reason = self._is_signal_valid_for_sprint(
                    signal_data,
                    current_time,
                )
                if not is_valid:
                    log(signal_not_actual(symbol, "sprint", reason))
                    return

            # Помечаем серию как активную
            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Антимартингейл"))

            await self._run_antimartingale_series(
                trade_key,
                symbol,
                timeframe,
                direction,
                log,
                signal_data["timestamp"],
                signal_data,
            )

        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(series_completed(symbol, timeframe, "Антимартингейл"))

    async def _run_antimartingale_series(
        self,
        trade_key: str,
        symbol: str,
        timeframe: str,
        initial_direction: int,
        log,
        signal_received_time: datetime,
        signal_data: dict,
    ):
        """Запускает серию Антимартингейла для конкретного сигнала (парлей)"""
        series_left = self._series_remaining.get(
            trade_key,
            int(self.params.get("repeat_count", 10)),
        )
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        step = 0
        did_place_any_trade = False

        # Счётчик подряд идущих не-WIN (PUSH / UNKNOWN)
        consecutive_non_win = 0

        series_direction = initial_direction
        max_steps = int(self.params.get("max_steps", 3))

        signal_at_str = signal_data.get("signal_time_str") or format_local_time(
            signal_received_time
        )
        series_label = self.format_series_label(trade_key, series_left=series_left)

        # Парлей: начинаем с базовой ставки и увеличиваем на размер профита после каждой победы
        base_stake = float(self.params.get("base_investment", 100))
        current_stake = base_stake

        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

            # Проверка актуальности только перед первой ставкой в серии
            if not did_place_any_trade:
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data,
                        current_time,
                        for_placement=True,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return
                else:
                    # Первая ставка sprint — строгие 5 секунд
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        {"timestamp": signal_received_time},
                        current_time,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return

            # Проверяем payout и баланс
            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            pct, balance = await self.check_payout_and_balance(
                symbol,
                current_stake,
                min_pct,
                wait_low,
            )
            if pct is None:
                continue

            log(
                trade_step(
                    symbol,
                    step,
                    format_amount(current_stake),
                    self._trade_minutes,
                    series_direction,
                    pct,
                )
            )

            # Финальная проверка актуальности перед размещением сделки
            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))

            if self._trade_type == "classic":
                original_max_age = self.params.get("classic_signal_max_age_sec", 170.0)

                # Окно: (consecutive_non_win + 1) * TF (в секундах)
                tf_minutes = _minutes_from_timeframe(timeframe)
                candles = max(1, 1 + consecutive_non_win)
                extended_max_age = candles * tf_minutes * 60
                self.params["classic_signal_max_age_sec"] = extended_max_age

                # Первая сделка (consecutive_non_win == 0) — учитываем next_expire.
                # Повторные (PUSH/UNKNOWN) — только возраст.
                for_placement_flag = consecutive_non_win == 0

                try:
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data,
                        current_time,
                        for_placement=for_placement_flag,
                    )
                finally:
                    self.params["classic_signal_max_age_sec"] = original_max_age

            else:
                # СПРИНТ: для повторных ставок мягкая проверка по окну
                is_valid, reason = self._is_sprint_signal_valid_for_series(
                    signal_data,
                    current_time,
                    consecutive_non_win=consecutive_non_win,
                )

            if not is_valid:
                log(signal_not_actual_for_placement(symbol, reason))
                return

            # Для classic: всегда следующая свеча от текущего момента
            if self._trade_type == "classic":
                self._next_expire_dt = self._calc_next_candle_from_now(timeframe)

            # Определяем режим аккаунта
            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            # Размещаем сделку
            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(
                symbol,
                series_direction,
                current_stake,
                self._anchor_ccy,
            )

            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return

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
                trade_id,
                symbol,
                timeframe,
                series_direction,
                current_stake,
                pct,
                trade_seconds,
                account_mode,
                expected_end_ts,
                signal_at=signal_at_str,
                series_label=series_label,
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

            # Ждём результат
            profit = await self.wait_for_trade_result(
                trade_id=trade_id,
                wait_seconds=float(wait_seconds),
                placed_at=datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                signal_at=signal_at_str,
                symbol=symbol,
                timeframe=timeframe,
                direction=series_direction,
                stake=float(current_stake),
                percent=int(pct),
                account_mode=account_mode,
                indicator=self._last_indicator,
                series_label=series_label,
            )

            # === Парлей-логика AntiMartingale ===
            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True) + " Серия завершается.")
                # считаем как LOSS, очередь StrategyCommon не очищаем
                break

            elif profit > 0:
                # WIN: увеличиваем ставку на размер профита, продолжаем серию
                log(win_with_parlay(symbol, format_amount(profit)))
                consecutive_non_win = 0  # стрик не-WIN обнулён

                # Очистка сигналов из очереди StrategyCommon после WIN
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(trade_result_removed(symbol, removed, "WIN"))

                current_stake += float(profit)
                step += 1  # продолжаем только после WIN

            elif abs(profit) < 1e-9:
                # PUSH: повторяем с той же ставкой, step не меняем
                log(push_repeat_same_stake(symbol))
                consecutive_non_win += 1

                # Очистка сигналов из очереди StrategyCommon после PUSH
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(trade_result_removed(symbol, removed, "PUSH"))

            else:
                # LOSS: серия Антимартина завершается, очередь не чистим
                log(loss_series_finish(symbol, format_amount(profit)))
                consecutive_non_win += 1
                break

            await self.sleep(0.2)

            # Для classic не двигаем _next_expire_dt вручную —
            # он каждый раз пересчитывается в _calc_next_candle_from_now.

        if did_place_any_trade:
            if step >= max_steps:
                log(steps_limit_reached(symbol, max_steps, flag="⛳"))
            series_left = max(0, series_left - 1)
            self._series_remaining[trade_key] = series_left
            log(series_remaining(symbol, series_left))
            self._check_all_series_completed(self._series_remaining)

    # =====================================================================
    # СЛУЖЕБНЫЕ
    # =====================================================================

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
        self,
        trade_id: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        trade_seconds: float,
        account_mode: str,
        expected_end_ts: float,
        *,
        signal_at: Optional[str] = None,
        series_label: Optional[str] = None,
    ):
        """Уведомляет о pending сделке"""
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
                )
            except Exception:
                pass

    def format_series_label(
        self,
        trade_key: str,
        *,
        series_left: int | None = None,
    ) -> str | None:
        if series_left is None:
            series_left = self._series_remaining.get(trade_key)
        return super().format_series_label(trade_key, series_left=series_left)

    def stop(self):
        """Остановка стратегии с очисткой активных серий"""
        super().stop()
        self._active_series.clear()
        self._series_remaining.clear()
        self._low_payout_queues.clear()

    def update_params(self, **params):
        """Обновление параметров стратегии"""
        super().update_params(**params)
        if "repeat_count" in params:
            self._series_remaining.clear()
