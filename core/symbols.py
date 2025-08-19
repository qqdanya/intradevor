# core/symbols.py
from __future__ import annotations

# Сначала длинные коды, чтобы ловить USDT перед USD
KNOWN_QUOTES = (
    "USDT",
    "USDC",
    "USD",
    "EUR",
    "RUB",
    "GBP",
    "JPY",
    "AUD",
    "NZD",
    "CAD",
    "CHF",
    "CNY",
    "CNH",
    "TRY",
    "PLN",
    "SEK",
)


def api_symbol(s: str) -> str:
    """EUR/USDT -> EURUSDT; eurusdt -> EURUSDT"""
    return s.replace("/", "").upper()


def split_symbol(s: str) -> tuple[str, str]:
    """EURUSDT/EUR/USDT -> ('EUR','USDT')"""
    raw = api_symbol(s)
    for q in KNOWN_QUOTES:
        if raw.endswith(q):
            return raw[: -len(q)], q
    # Фоллбек: считаем базой всё, кроме последних 3 символов
    return raw[:-3], raw[-3:]


def ui_symbol(s: str) -> str:
    """EURUSDT -> EUR/USDT"""
    base, quote = split_symbol(s)
    return f"{base}/{quote}"
