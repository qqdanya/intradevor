# Константы для всех стратегий
ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"
CLASSIC_ALLOWED_TFS = {"M5", "M15", "M30", "H1", "H4"}
CLASSIC_SIGNAL_MAX_AGE_SEC = 170.0  # 2 минуты 50 секунд (next_timestamp - 3m + 10s запас)
CLASSIC_TRADE_BUFFER_SEC = 10.0     # 10 секунд на размещение ставки
CLASSIC_MIN_TIME_BEFORE_NEXT_SEC = 180.0  # 3 минуты до следующей свечи
SPRINT_SIGNAL_MAX_AGE_SEC = 10.0
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
    "classic_signal_max_age_sec": 170.0,
    "classic_trade_buffer_sec": 10.0,
    "classic_min_time_before_next_sec": 180.0,
    # По умолчанию объединяем серию для всех сигналов, чтобы UI и стартовые
    # параметры стратегии были согласованы (чекбокс включён).
    "use_common_series": True,
    "auto_minutes": False,
}
