from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QSpinBox,
    QDoubleSpinBox,
    QDialogButtonBox,
    QCheckBox,
    QLabel,
)
from strategies.martingale import _minutes_from_timeframe
from core.policy import normalize_sprint


class MartingaleSettingsDialog(QDialog):
    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки: Martingale")
        self.params = params.copy()

        # ---- НОВОЕ: минуты экспирации ----
        tf = str(self.params.get("timeframe", "M1"))
        symbol = str(self.params.get("symbol", ""))  # прокинут из MainWindow

        default_minutes = int(self.params.get("minutes", _minutes_from_timeframe(tf)))

        self.minutes = QSpinBox()
        # Для BTC — минимум 5, для остальных — 1 (но 2 всё равно отфильтруем перед сохранением)
        self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
        self.minutes.setValue(default_minutes)

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

        self.coefficient = QDoubleSpinBox()
        self.coefficient.setRange(1.0, 10.0)
        self.coefficient.setSingleStep(0.1)
        self.coefficient.setValue(self.params.get("coefficient", 2.0))

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
        form.addRow("Макс. шагов", self.max_steps)
        form.addRow("Повторов серии", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Коэффициент", self.coefficient)
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
        # Мягкая нормализация по policy: 2 → 3 (для не-BTC), <5 → 5 (для BTC), и т.д.
        symbol = str(self.params.get("symbol", ""))
        m = int(self.minutes.value())
        norm = normalize_sprint(symbol, m)
        if norm is None:
            norm = 5 if symbol == "BTCUSDT" else (1 if m < 3 else max(3, min(500, m)))

        return {
            "minutes": int(norm),  # ⬅️ НОВОЕ: вернули минуты
            "base_investment": self.base_investment.value(),
            "max_steps": self.max_steps.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "coefficient": self.coefficient.value(),
            "min_percent": self.min_percent.value(),
            "allow_parallel_trades": bool(self.parallel_trades.isChecked()),
        }
