# strategies/oscar_grind_1.py
from __future__ import annotations

from strategies.oscar_grind_2 import OscarGrind2Strategy
from core.money import format_amount


class OscarGrind1Strategy(OscarGrind2Strategy):
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
