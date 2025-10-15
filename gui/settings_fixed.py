from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QSpinBox,
    QDialogButtonBox,
    QCheckBox,
    QLabel,
)
from strategies.martingale import _minutes_from_timeframe
from core.policy import normalize_sprint


class FixedSettingsDialog(QDialog):
    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки: Фиксированная ставка")
        self.params = params.copy()

        tf = str(self.params.get("timeframe", "M1"))
        symbol = str(self.params.get("symbol", ""))
        default_minutes = int(self.params.get("minutes", _minutes_from_timeframe(tf)))

        self.minutes = QSpinBox()
        self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
        self.minutes.setValue(default_minutes)

        self.base_investment = QSpinBox()
        self.base_investment.setRange(1, 50000)
        self.base_investment.setValue(self.params.get("base_investment", 100))

        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 1000)
        self.repeat_count.setValue(self.params.get("repeat_count", 10))

        self.min_balance = QSpinBox()
        self.min_balance.setRange(1, 10_000_000)
        self.min_balance.setValue(self.params.get("min_balance", 100))

        self.min_percent = QSpinBox()
        self.min_percent.setRange(0, 100)
        self.min_percent.setValue(self.params.get("min_percent", 70))

        self.parallel_trades = QCheckBox()
        self.parallel_trades.setChecked(
            bool(self.params.get("allow_parallel_trades", True))
        )
        parallel_label = QLabel("Обрабатывать множество сигналов")
        parallel_label.mousePressEvent = lambda event: self.parallel_trades.toggle()

        form = QFormLayout()
        form.addRow("Базовая ставка", self.base_investment)
        form.addRow("Время экспирации (мин)", self.minutes)
        form.addRow("Количество ставок", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Мин. процент", self.min_percent)
        form.addRow(parallel_label, self.parallel_trades)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        form.addRow(btns)
        self.setLayout(form)

    def get_params(self) -> dict:
        symbol = str(self.params.get("symbol", ""))
        m = int(self.minutes.value())
        norm = normalize_sprint(symbol, m)
        if norm is None:
            norm = 5 if symbol == "BTCUSDT" else (1 if m < 3 else max(3, min(500, m)))
        return {
            "minutes": int(norm),
            "base_investment": self.base_investment.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "min_percent": self.min_percent.value(),
            "allow_parallel_trades": bool(self.parallel_trades.isChecked()),
        }

