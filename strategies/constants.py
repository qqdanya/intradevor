# Константы для всех стратегий

ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"
CLASSIC_ALLOWED_TFS = {"M5", "M15", "M30", "H1", "H4"}

CLASSIC_SIGNAL_MAX_AGE_SEC = 120.0
SPRINT_SIGNAL_MAX_AGE_SEC = 5.0

MOSCOW_TZ = "Europe/Moscow"

# Параметры по умолчанию для всех стратегий
DEFAULTS = {
    "base_investment": 100,
    "min_balance": 100,
    "min_percent": 70,
    "wait_on_low_percent": 1,
    "signal_timeout_sec": 3600,
    "account_currency": "RUB",
    "result_wait_s": 60.0,
    "grace_delay_sec": 30.0,
    "trade_type": "classic",
    "allow_parallel_trades": True,
}
