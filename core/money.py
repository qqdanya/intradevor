# core/money.py
from __future__ import annotations

_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
}


def format_money(amount: float, code: str) -> str:
    """1234.5, 'RUB' -> '1 234.50 ₽'; если код не знаем — '1 234.50 XXX'"""
    s = f"{amount:,.2f}".replace(",", " ")
    sym = _SYMBOLS.get(code.upper())
    return f"{s} {sym}" if sym else f"{s} {code.upper()}"
