# core/money.py
from __future__ import annotations

_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
}


def format_amount(amount: float, show_plus: bool = False) -> str:
    """Возвращает строку с пробелами между тысячами и запятой в качестве разделителя."""
    s = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    if show_plus and amount > 0:
        s = "+" + s
    return s


def format_money(amount: float, code: str, *, show_plus: bool = False) -> str:
    """1234.5, 'RUB' -> '1 234,50 ₽'; если код не знаем — '1 234,50 XXX'"""
    s = format_amount(amount, show_plus=show_plus)
    sym = _SYMBOLS.get(code.upper())
    return f"{s} {sym}" if sym else f"{s} {code.upper()}"
