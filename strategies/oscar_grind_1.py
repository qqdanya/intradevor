# strategies/oscar_grind_1.py
from __future__ import annotations

from typing import Optional

from strategies.oscar_grind_strategy import OscarGrindStrategy
from core.money import format_amount
from strategies.log_messages import oscar_win_basic, oscar_refund, oscar_loss


class OscarGrind1Strategy(OscarGrindStrategy):
    """Oscar Grind 1 (упрощённая версия)"""

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
        super().__init__(
            http_client=http_client,
            user_id=user_id,
            user_hash=user_hash,
            symbol=symbol,
            log_callback=log_callback,
            timeframe=timeframe,
            params=params,
            **kwargs,
        )
        # чтобы стратегия в UI называлась именно так
        self.strategy_name = "OscarGrind1"

    def _next_stake(
        self,
        *,
        outcome: str,
        stake: float,
        base_unit: float,
        pct: float,
        need: float,
        profit: float,
        cum_profit: float,
    ) -> float:
        log = self.log or (lambda s: None)

        if outcome == "win":
            next_stake = float(stake) + float(base_unit)
            log(
                oscar_win_basic(
                    self.symbol,
                    format_amount(profit),
                    format_amount(cum_profit),
                    format_amount(base_unit),
                    format_amount(next_stake),
                )
            )
            return float(next_stake)

        # refund / loss
        next_stake = float(stake)
        if outcome == "refund":
            log(oscar_refund(self.symbol, format_amount(next_stake)))
        else:
            log(oscar_loss(self.symbol, format_amount(profit), format_amount(next_stake)))
        return float(next_stake)
