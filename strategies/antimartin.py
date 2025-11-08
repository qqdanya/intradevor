from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from strategies.constants import MOSCOW_TZ
from core.time_utils import format_local_time
from core.money import format_amount
from core.intrade_api_async import is_demo_account
from strategies.log_messages import (
    repeat_count_empty,
    series_already_active,
    signal_not_actual,
    signal_not_actual_for_placement,
    start_processing,
    trade_placement_failed,
    result_unknown,
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
      - Из очереди сигналов удаляем после WIN или PUSH (refund).
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

        self._active_series: dict[str, bool] = {}
        self._series_remaining: dict[str, int] = {}

    def is_series_active(self, trade_key: str) -> bool:
        return self._active_series.get(trade_key, False)

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Антимартингейла"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']

        log = self.log or (lambda s: None)

        trade_key = f"{symbol}_{timeframe}"
        if trade_key in self._active_series and self._active_series[trade_key]:
            log(series_already_active(symbol, timeframe))
            if hasattr(self, '_common'):
                await self._common._handle_pending_signal(trade_key, signal_data)
            return

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
            # метаданные сигнала
            self._last_signal_ver = signal_data['version']
            self._last_indicator = signal_data['indicator']
            self._last_signal_at_str = format_local_time(signal_data['timestamp'])

            ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
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

            current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
            if self._trade_type == "classic":
                is_valid, reason = self._is_signal_valid_for_classic(signal_data, current_time, for_placement=True)
                if not is_valid:
                    log(signal_not_actual(symbol, "classic", reason))
                    return
            else:
                is_valid, reason = self._is_signal_valid_for_sprint(signal_data, current_time)
                if not is_valid:
                    log(signal_not_actual(symbol, "sprint", reason))
                    return

            self._active_series[trade_key] = True
            series_started = True
            log(start_processing(symbol, "Антимартингейл"))

            await self._run_antimartingale_series(
                trade_key, symbol, timeframe, direction, log, signal_data['timestamp'], signal_data
            )

        finally:
            if series_started:
                self._active_series.pop(trade_key, None)
                log(f"[{symbol}] Серия Антимартингейла завершена для {timeframe}")

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
        series_left = self._series_remaining.get(trade_key, int(self.params.get("repeat_count", 10)))
        if series_left <= 0:
            log(repeat_count_empty(symbol, series_left))
            return

        step = 0
        did_place_any_trade = False
        needs_signal_validation = True
        series_direction = initial_direction
        max_steps = int(self.params.get("max_steps", 3))

        signal_at_str = signal_data.get('signal_time_str') or format_local_time(signal_received_time)

        # парлей: начинаем с базовой ставки и увеличиваем на размер профита после каждой победы
        base_stake = float(self.params.get("base_investment", 100))
        current_stake = base_stake

        while self._running and step < max_steps:
            await self._pause_point()
            if not await self.ensure_account_conditions():
                continue

            # валидируем сигнал только перед первой ставкой серии
            if needs_signal_validation:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
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
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        {'timestamp': signal_received_time},
                        current_time,
                    )
                    if not is_valid:
                        log(signal_not_actual_for_placement(symbol, reason))
                        return

            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            pct, balance = await self.check_payout_and_balance(symbol, current_stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step} stake={format_amount(current_stake)} min={self._trade_minutes} "
                f"side={'UP' if series_direction == 1 else 'DOWN'} payout={pct}%")

            # Финальная проверка актуальности перед размещением сделки
            if needs_signal_validation:
                current_time = datetime.now(ZoneInfo(MOSCOW_TZ))
                if self._trade_type == "classic":
                    is_valid, reason = self._is_signal_valid_for_classic(
                        signal_data,
                        current_time,
                        for_placement=True,
                    )
                else:
                    sprint_payload = signal_data
                    if not sprint_payload.get('timestamp'):
                        sprint_payload = {'timestamp': signal_received_time}
                    is_valid, reason = self._is_signal_valid_for_sprint(
                        sprint_payload,
                        current_time,
                    )

                if not is_valid:
                    log(signal_not_actual_for_placement(symbol, reason))
                    return

                needs_signal_validation = False

            try:
                demo_now = await is_demo_account(self.http_client)
            except Exception:
                demo_now = False
            account_mode = "ДЕМО" if demo_now else "РЕАЛ"

            self._status("делает ставку")
            trade_id = await self.place_trade_with_retry(symbol, series_direction, current_stake, self._anchor_ccy)
            if not trade_id:
                log(trade_placement_failed(symbol, "Пропускаем сигнал."))
                return

            did_place_any_trade = True

            trade_seconds, expected_end_ts = self._calculate_trade_duration(symbol)
            wait_seconds = self.params.get("result_wait_s")
            wait_seconds = trade_seconds if wait_seconds is None else float(wait_seconds)

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
            )
            self._register_pending_trade(trade_id, symbol, timeframe)

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
            )

            # === Парлей-логика ===
            if profit is None:
                log(result_unknown(symbol, treat_as_loss=True) + " Серия завершается.")
                break
            elif profit > 0:
                log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Увеличиваем ставку на размер выигрыша (парлей).")
                # очистка отложенных сигналов после WIN
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(f"[{symbol}] 🗑 Удалено сигналов из очередей после WIN: {removed}")
                current_stake += float(profit)  # парлей
                step += 1  # продолжаем только после WIN
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без изменения ставки.")
                if hasattr(self, "_common") and self._common is not None:
                    removed = self._common.discard_signals_for(trade_key)
                    if removed:
                        log(f"[{symbol}] 🗑 Удалено сигналов из очередей после PUSH: {removed}")
                # step не меняем — повторим с тем же current_stake
            else:
                log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Серия завершается.")
                # при LOSS в Антисерии — стоп; очередь не очищаем
                break

            await self.sleep(0.2)

            if self._trade_type == "classic" and self._next_expire_dt is not None:
                self._next_expire_dt += timedelta(minutes=_minutes_from_timeframe(timeframe))

        if did_place_any_trade:
            if step >= max_steps:
                log(f"[{symbol}] ⛳ Достигнут лимит шагов (max_steps={max_steps}). Серия остановлена.")
            series_left = max(0, series_left - 1)
            self._series_remaining[trade_key] = series_left
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0, (self._next_expire_dt - datetime.now(ZoneInfo(MOSCOW_TZ))).total_seconds()
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
    ):
        placed_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
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
                )
            except Exception:
                pass

    def stop(self):
        super().stop()
        self._active_series.clear()
        self._series_remaining.clear()

    def update_params(self, **params):
        super().update_params(**params)
        if "repeat_count" in params:
            self._series_remaining.clear()
