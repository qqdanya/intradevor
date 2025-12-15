from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QSpinBox,
    QDialogButtonBox,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QWidget,
)
from strategies.timeframe_utils import minutes_from_timeframe
from core.policy import normalize_sprint


class FibonacciSettingsDialog(QDialog):
    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки: Fibonacci")
        self.params = params.copy()

        tf = str(self.params.get("timeframe", "M1"))
        symbol = str(self.params.get("symbol", ""))
        default_minutes = int(self.params.get("minutes", minutes_from_timeframe(tf)))

        self.minutes = QSpinBox()
        self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
        self.minutes.setValue(default_minutes)

        self.auto_minutes = QCheckBox("Авто")
        self.auto_minutes.setChecked(bool(self.params.get("auto_minutes", True)))
        self.auto_minutes.toggled.connect(
            lambda checked: self.minutes.setEnabled(not checked)
        )

        self.base_investment = QSpinBox()
        self.base_investment.setRange(1, 50000)
        self.base_investment.setValue(self.params.get("base_investment", 100))

        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 20)
        self.max_steps.setValue(self.params.get("max_steps", 5))

        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 1000)
        self.repeat_count.setValue(self.params.get("repeat_count", 10))

        self.min_balance = QSpinBox()
        self.min_balance.setRange(1, 10_000_000)
        self.min_balance.setValue(self.params.get("min_balance", 100))

        self.min_percent = QSpinBox()
        self.min_percent.setRange(0, 100)
        self.min_percent.setValue(self.params.get("min_percent", 70))

        allow_parallel = bool(self.params.get("allow_parallel_trades", True))
        use_common = bool(self.params.get("use_common_series", True))
        single_series = use_common or not allow_parallel
        self.series_mode = QComboBox()
        self.series_mode.addItems(
            ["Вести единую серию", "Обрабатывать множество сигналов"]
        )
        self.series_mode.setCurrentIndex(0 if single_series else 1)

        minutes_row = QWidget()
        minutes_layout = QHBoxLayout(minutes_row)
        minutes_layout.setContentsMargins(0, 0, 0, 0)
        minutes_layout.setSpacing(6)
        minutes_layout.addWidget(self.minutes)
        minutes_layout.addWidget(self.auto_minutes)
        minutes_layout.addStretch(1)

        form = QFormLayout()
        form.addRow("Базовая ставка", self.base_investment)
        form.addRow("Время экспирации (мин)", minutes_row)
        form.addRow("Макс. шагов", self.max_steps)
        form.addRow("Повторов серии", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Мин. процент", self.min_percent)
        form.addRow("Режим сигналов", self.series_mode)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        form.addRow(btns)
        self.setLayout(form)

    def get_params(self) -> dict:
        symbol = str(self.params.get("symbol", ""))
        auto_minutes = bool(self.auto_minutes.isChecked())
        raw_minutes = minutes_from_timeframe(tf) if auto_minutes else int(self.minutes.value())
        norm = normalize_sprint(symbol, raw_minutes)
        if norm is None:
            norm = 5 if symbol == "BTCUSDT" else (1 if raw_minutes < 3 else max(3, min(500, raw_minutes)))
        return {
            "minutes": int(norm),
            "auto_minutes": auto_minutes,
            "base_investment": self.base_investment.value(),
            "max_steps": self.max_steps.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "min_percent": self.min_percent.value(),
            "allow_parallel_trades": self.series_mode.currentIndex() == 1,
            "use_common_series": self.series_mode.currentIndex() == 0,
        }
