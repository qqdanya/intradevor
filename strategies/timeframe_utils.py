# strategies/timeframe_utils.py
from __future__ import annotations


def minutes_from_timeframe(tf: str) -> int:
    """Конвертация таймфрейма в минуты (M1/H1/D1/W1)."""
    if not tf:
        return 1
    unit = tf[0].upper()
    try:
        n = int(tf[1:])
    except Exception:
        return 1

    if unit == "M":
        return n
    if unit == "H":
        return n * 60
    if unit == "D":
        return n * 60 * 24
    if unit == "W":
        return n * 60 * 24 * 7
    return 1
