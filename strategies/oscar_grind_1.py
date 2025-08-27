# strategies/oscar_grind_1.py
from __future__ import annotations

from strategies.oscar_grind_2 import OscarGrind2Strategy


class OscarGrind1Strategy(OscarGrind2Strategy):
    """Классическая стратегия Oscar Grind.

    В этом варианте после выигрыша следующая ставка всегда
    увеличивается на величину базовой ставки (unit) без ограничения
    по достижению целевой прибыли серии.
    """

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
        target_profit: float,
        log,
    ) -> float:
        if outcome == "win":
            next_stake = stake + base_unit
            log(
                f"[{self.symbol}] ✅ WIN: profit={profit:.2f}. "
                f"Накоплено {cum_profit:.2f}/{target_profit:.2f}. "
                f"Следующая ставка = stake+unit → {next_stake:.2f}"
            )
        else:
            next_stake = stake
            if outcome == "refund":
                log(
                    f"[{self.symbol}] ↩️ REFUND: ставка возвращена. "
                    f"Следующая ставка остаётся {next_stake:.2f}."
                )
            else:
                log(
                    f"[{self.symbol}] ❌ LOSS: profit={profit:.2f}. "
                    f"Следующая ставка остаётся {next_stake:.2f}."
                )
        return float(next_stake)

