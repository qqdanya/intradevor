from PyQt6.QtWidgets import QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, QDialogButtonBox

class MartingaleSettingsDialog(QDialog):
    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки: Martingale")
        self.params = params.copy()

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

        self.wait_on_low_percent = QSpinBox()
        self.wait_on_low_percent.setRange(0, 60)
        self.wait_on_low_percent.setValue(self.params.get("wait_on_low_percent", 1))

        form = QFormLayout()
        form.addRow("Базовая ставка", self.base_investment)
        form.addRow("Макс. шагов", self.max_steps)
        form.addRow("Повторов серии", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Коэффициент", self.coefficient)
        form.addRow("Мин. процент", self.min_percent)
        form.addRow("Ожид. при низком % (с)", self.wait_on_low_percent)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        form.addRow(btns)
        self.setLayout(form)

    def get_params(self) -> dict:
        return {
            "base_investment": self.base_investment.value(),
            "max_steps": self.max_steps.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "coefficient": self.coefficient.value(),
            "min_percent": self.min_percent.value(),
            "wait_on_low_percent": self.wait_on_low_percent.value(),
        }
