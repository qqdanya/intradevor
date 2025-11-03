from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from strategies.base_trading_strategy import BaseTradingStrategy, _minutes_from_timeframe
from core.money import format_amount
from core.intrade_api_async import is_demo_account

MARTINGALE_DEFAULTS = {
    "base_investment": 100,
    "max_steps": 5,
    "repeat_count": 10,
    "coefficient": 2.0,
}


class MartingaleStrategy(BaseTradingStrategy):
    """Стратегия Мартингейла"""
    
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
        martingale_params = dict(MARTINGALE_DEFAULTS)
        if params:
            martingale_params.update(params)
            
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=martingale_params,
            strategy_name="Martingale",
            **kwargs,
        )

    async def _process_single_signal(self, signal_data: dict):
        """Обработка одного сигнала для Мартингейла"""
        symbol = signal_data['symbol']
        timeframe = signal_data['timeframe']
        direction = signal_data['direction']
        
        log = self.log or (lambda s: None)
        log(f"[{symbol}] Начало обработки сигнала (Мартингейл)")
        
        # Обновляем информацию о сигнале
        self._last_signal_ver = signal_data['version']
        self._last_indicator = signal_data['indicator']
        self._last_signal_at_str = signal_data['timestamp'].strftime("%d.%m.%Y %H:%M:%S")
        
        ts = signal_data['meta'].get('next_timestamp') if signal_data['meta'] else None
        self._next_expire_dt = ts.astimezone(ZoneInfo("Europe/Moscow")) if ts else None

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

        # Запускаем серию Мартингейла
        await self._run_martingale_series(symbol, timeframe, direction, log)

    async def _run_martingale_series(self, symbol: str, timeframe: str, initial_direction: int, log):
        """Запускает серию Мартингейла для конкретного сигнала"""
        series_left = int(self.params.get("repeat_count", 10))
        if series_left <= 0:
            log(f"[{symbol}] 🛑 repeat_count={series_left} — нечего выполнять.")
            return

        step = 0
        did_place_any_trade = False
        series_direction = initial_direction
        max_steps = int(self.params.get("max_steps", 5))

        while self._running and step < max_steps:
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

            # Рассчитываем ставку
            base_stake = float(self.params.get("base_investment", 100))
            coeff = float(self.params.get("coefficient", 2.0))
            stake = base_stake * (coeff ** step) if step > 0 else base_stake

            min_pct = int(self.params.get("min_percent", 70))
            wait_low = float(self.params.get("wait_on_low_percent", 1))

            # Проверяем выплату и баланс
            pct, balance = await self.check_payout_and_balance(symbol, stake, min_pct, wait_low)
            if pct is None:
                continue

            log(f"[{symbol}] step={step} stake={format_amount(stake)} min={self._trade_minutes} "
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

            # Обрабатываем результат
            if profit is None:
                log(f"[{symbol}] ⚠ Результат неизвестен — считаем как LOSS.")
                step += 1
            elif profit > 0:
                log(f"[{symbol}] ✅ WIN: profit={format_amount(profit)}. Серия завершена.")
                break
            elif abs(profit) < 1e-9:
                log(f"[{symbol}] 🤝 PUSH: возврат ставки. Повтор шага без увеличения.")
            else:
                log(f"[{symbol}] ❌ LOSS: profit={format_amount(profit)}. Увеличиваем ставку.")
                step += 1

            await self.sleep(0.2)

            # Обновляем время экспирации для classic
            if self._trade_type == "classic" and self._next_expire_dt is not None:
                from datetime import timedelta
                self._next_expire_dt += timedelta(
                    minutes=_minutes_from_timeframe(timeframe)
                )

        if did_place_any_trade:
            if step >= max_steps:
                log(f"[{symbol}] 🛑 Достигнут лимит шагов ({max_steps}).")
            series_left -= 1
            log(f"[{symbol}] ▶ Осталось серий: {series_left}")

    def _calculate_trade_duration(self, symbol: str) -> tuple[float, float]:
        """Рассчитывает длительность сделки"""
        from datetime import datetime
        if self._trade_type == "classic" and self._next_expire_dt is not None:
            trade_seconds = max(
                0.0,
                (self._next_expire_dt - datetime.now(ZoneInfo("Europe/Moscow"))).total_seconds(),
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
