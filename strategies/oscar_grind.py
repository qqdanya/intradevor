# strategies/oscar_grind.py
from __future__ import annotations

import asyncio
from typing import Optional, Sequence

from core.http_async import HttpClient
from core.intrade_api_async import (
    get_balance_info,
    get_current_percent,
    place_trade,
    check_trade_result,
    is_demo_account,
)
from core.signal_waiter import wait_for_signal
from strategies.base import StrategyBase

ALL_SYMBOLS_LABEL = "Все валютные пары"

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

        # если выбран режим "Все валютные пары" — список конкретных пар обязателен
        symbols: Sequence[str] = self.params.get("symbols", [])
        self._all_symbols = [s for s in symbols if s != ALL_SYMBOLS_LABEL]

        self.http_client = http_client
        self.timeframe = timeframe or self.params.get("timeframe", "M1")
        self.params["timeframe"] = self.timeframe

        anchor = str(self.params.get("account_currency", DEFAULTS["account_currency"])).upper()
        self._anchor_ccy = anchor
        self.params["account_currency"] = anchor
        self._anchor_is_demo: Optional[bool] = None

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

    async def _wait_any_signal(self, timeout: float) -> tuple[str, int]:
        """Ожидает первый сигнал среди всех доступных пар."""
        tasks = {}
        loop = asyncio.get_running_loop()
        for sym in self._all_symbols:
            task = loop.create_task(
                wait_for_signal(
                    sym,
                    self.timeframe,
                    check_pause=self._pause_point,
                    timeout=None,
                )
            )
            tasks[task] = sym

        try:
            done, pending = await asyncio.wait(
                tasks.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise asyncio.TimeoutError
            finished = done.pop()
            symbol = tasks[finished]
            direction = finished.result()
            return symbol, direction
        finally:
            for t in tasks.keys():
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks.keys(), return_exceptions=True)

    async def run(self) -> None:
        self._running = True
        log = self.log or (lambda s: None)

        try:
            self._anchor_is_demo = await is_demo_account(self.http_client)
            mode_txt = "ДЕМО" if self._anchor_is_demo else "РЕАЛ"
            log(f"[{self.symbol}] Якорный режим счёта: {mode_txt}")
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось определить режим счёта при старте: {e}")
            self._anchor_is_demo = False

        try:
            amount, cur_ccy, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            log(
                f"[{self.symbol}] Баланс: {display} ({amount:.2f}), текущая валюта: {cur_ccy}, якорь: {self._anchor_ccy}"
            )
        except Exception as e:
            log(f"[{self.symbol}] ⚠ Не удалось получить баланс при старте: {e}")

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
        account_ccy = self._anchor_ccy
        minutes = int(self.params.get("minutes", DEFAULTS["minutes"]))
        result_wait_s = float(
            self.params.get("result_wait_s", DEFAULTS["result_wait_s"])
        )

        series_left = repeat_count

        while self._running and series_left > 0:
            await self._pause_point()

            if not await self._ensure_anchor_currency():
                continue
            if not await self._ensure_anchor_account_mode():
                continue

            # Проверка баланса перед серией
            try:
                bal, _, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
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

                if not await self._ensure_anchor_currency():
                    continue
                if not await self._ensure_anchor_account_mode():
                    continue

                if self.symbol == ALL_SYMBOLS_LABEL:
                    try:
                        sym, status = await self._wait_any_signal(signal_timeout)
                    except asyncio.TimeoutError:
                        log(f"[{self.symbol}] ⏱️ Таймаут ожидания сигнала")
                        continue
                    pct = await get_current_percent(
                        self.http_client, stake, sym, minutes, account_ccy
                    )
                    if pct is None or pct < min_percent:
                        await self.sleep(wait_low)
                        continue
                else:
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
                        sym = self.symbol
                    except asyncio.TimeoutError:
                        log(f"[{self.symbol}] ⏱️ Таймаут ожидания сигнала")
                        continue

                self._status("ожидание результата")

                trade_id = await place_trade(
                    self.http_client,
                    self.user_id,
                    self.user_hash,
                    stake,
                    sym,
                    status,
                    minutes,
                    account_ccy=account_ccy,
                    strict=False,
                    on_log=log,
                )
                if not trade_id:
                    log(f"[{sym}] ❌ Сделка не размещена. Пауза и повтор.")
                    await self.sleep(1.0)
                    continue

                # GUI: уведомляем о размещении
                if callable(self._on_trade_pending):
                    from datetime import datetime

                    placed_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    try:
                        self._on_trade_pending(
                            trade_id=trade_id,
                            symbol=sym,
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
                            symbol=sym,
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
                        f"[{sym}] ⚠ Результат неизвестен — считаем как убыточный."
                    )
                    profit = -float(stake)

                log(
                    f"[{sym}] Сделка {trade_number}: {'ПРИБЫЛЬ' if profit > 0 else 'УБЫТОК'} {profit:.2f}"
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
