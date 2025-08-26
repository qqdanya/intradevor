# strategies/oscar_grind.py
from __future__ import annotations

import asyncio
from typing import Optional

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance,
    get_current_percent,
    place_trade,
    check_trade_result,
)
from core.signal_waiter import wait_for_signal
from strategies.base import StrategyBase

DEFAULTS = {
    "base_investment": 100,
    "repeat_count": 10,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "minutes": 1,
    "result_wait_s": 60.0,
}


class OscarGrindStrategy(StrategyBase):
    """"Оскар Грайнд" — управление ставками с целью получить
    фиксированную прибыль за серию сделок.

    Стратегия стремится заработать ``base_investment`` за серию.
    При проигрыше ставка сохраняется, при выигрыше увеличивается на
    ``base_investment`` (но не больше необходимого для достижения цели).
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
        **_,
    ):
        p = dict(DEFAULTS)
        if params:
            p.update(params)

        super().__init__(
            session=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            **p,
        )

        self.http_client = http_client
        self.timeframe = timeframe or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        self._on_trade_result = self.params.get("on_trade_result")
        self._on_trade_pending = self.params.get("on_trade_pending")
        self._on_status = self.params.get("on_status")

        def _status(msg: str):
            cb = self._on_status
            if callable(cb):
                try:
                    cb(msg)
                except Exception:
                    pass

        self._status = _status

    async def run(self) -> None:
        self._running = True
        log = self.log or (lambda s: None)

        base = float(self.params.get("base_investment", DEFAULTS["base_investment"]))
        repeat_count = int(self.params.get("repeat_count", DEFAULTS["repeat_count"]))
        min_balance = float(self.params.get("min_balance", DEFAULTS["min_balance"]))
        min_percent = int(self.params.get("min_percent", DEFAULTS["min_percent"]))
        wait_low = float(
            self.params.get("wait_on_low_percent", DEFAULTS["wait_on_low_percent"])
        )
        signal_timeout = float(
            self.params.get("signal_timeout_sec", DEFAULTS["signal_timeout_sec"])
        )
        account_ccy = self.params.get(
            "account_currency", DEFAULTS["account_currency"]
        )
        minutes = int(self.params.get("minutes", DEFAULTS["minutes"]))
        result_wait_s = float(
            self.params.get("result_wait_s", DEFAULTS["result_wait_s"])
        )

        series_left = repeat_count

        while self._running and series_left > 0:
            await self._pause_point()

            # Проверка баланса перед серией
            try:
                bal = await get_balance(self.http_client, self.user_id, self.user_hash)
                if bal < min_balance:
                    log(
                        f"[{self.symbol}] 🛑 Баланс {bal:.2f} ниже минимального {min_balance:.2f}"
                    )
                    break
            except Exception:
                pass

            total_profit = 0.0
            stake = base
            trade_number = 1
            self._status("ожидание сигнала")

            log(
                f"[{self.symbol}] 🔁 Новая серия Оскар Грайнд. Цель профита: {base:.2f}"
            )

            while self._running and total_profit < base:
                await self._pause_point()

                pct = await get_current_percent(
                    self.http_client,
                    stake,
                    self.symbol,
                    minutes,
                    account_ccy,
                )
                if pct is None or pct < min_percent:
                    await self.sleep(wait_low)
                    continue

                try:
                    status = await wait_for_signal(
                        self.symbol,
                        self.timeframe,
                        check_pause=self._pause_point,
                        timeout=signal_timeout,
                    )
                except asyncio.TimeoutError:
                    log(f"[{self.symbol}] ⏱️ Таймаут ожидания сигнала")
                    continue

                self._status("ожидание результата")

                trade_id = await place_trade(
                    self.http_client,
                    self.user_id,
                    self.user_hash,
                    stake,
                    self.symbol,
                    status,
                    minutes,
                    account_ccy=account_ccy,
                    strict=False,
                    on_log=log,
                )
                if not trade_id:
                    log(f"[{self.symbol}] ❌ Сделка не размещена. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue

                # GUI: уведомляем о размещении
                if callable(self._on_trade_pending):
                    from datetime import datetime

                    placed_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=None,
                            placed_at=placed_at,
                            direction=status,
                            stake=float(stake),
                            percent=int(pct),
                            wait_seconds=float(result_wait_s),
                            account_mode=None,
                            indicator=None,
                        )
                    except Exception:
                        pass

                profit = await check_trade_result(
                    self.http_client,
                    user_id=self.user_id,
                    user_hash=self.user_hash,
                    trade_id=trade_id,
                    wait_time=result_wait_s,
                )

                if callable(self._on_trade_result):
                    from datetime import datetime

                    placed_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_result(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            timeframe=self.timeframe,
                            signal_at=None,
                            placed_at=placed_at,
                            direction=status,
                            stake=float(stake),
                            percent=int(pct),
                            profit=(
                                None if profit is None else float(profit)
                            ),
                            account_mode=None,
                            indicator=None,
                        )
                    except Exception:
                        pass

                if profit is None:
                    log(
                        f"[{self.symbol}] ⚠ Результат неизвестен — считаем как убыточный."
                    )
                    profit = -float(stake)

                log(
                    f"[{self.symbol}] Сделка {trade_number}: {'ПРИБЫЛЬ' if profit > 0 else 'УБЫТОК'} {profit:.2f}"
                )

                total_profit += profit

                if profit > 0:
                    remaining = base - total_profit
                    if remaining <= 0:
                        stake = base
                    else:
                        needed = remaining / (pct / 100.0)
                        stake = max(base, min(stake + base, needed))
                else:
                    stake = max(base, stake)

                trade_number += 1
                self._status("ожидание сигнала")

            series_left -= 1
            log(
                f"[{self.symbol}] Серия завершена. Осталось повторов: {series_left}"
            )

        self._running = False
        self._status("завершен")
        log(f"[{self.symbol}] Завершение стратегии.")
