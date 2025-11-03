from __future__ import annotations

from typing import Optional
from strategies.oscar_grind_base import OscarGrindBaseStrategy
from core.money import format_amount


class OscarGrind1Strategy(OscarGrindBaseStrategy):
    """Oscar Grind 1 стратегия (упрощенная версия)"""
    
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
            strategy_name="OscarGrind1",
            **kwargs,
        )

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
        log,
    ) -> float:
        if outcome == "win":
            next_stake = stake + base_unit
            log(
                f"[{self.symbol}] ✅ WIN: profit={format_amount(profit)}. "
                f"Накоплено {format_amount(cum_profit)}/{format_amount(base_unit)}. "
                f"Следующая ставка = stake+unit → {format_amount(next_stake)}"
            )
        else:
            next_stake = stake
            if outcome == "refund":
                log(
                    f"[{self.symbol}] ↩️ REFUND: ставка возвращена. "
                    f"Следующая ставка остаётся {format_amount(next_stake)}."
                )
            else:
                log(
                    f"[{self.symbol}] ❌ LOSS: profit={format_amount(profit)}. "
                    f"Следующая ставка остаётся {format_amount(next_stake)}."
                )
        return float(next_stake)
