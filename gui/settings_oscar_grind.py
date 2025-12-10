# gui/settings_oscar_grind.py
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


class OscarGrindSettingsDialog(QDialog):
    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки: Oscar Grind")
        self.params = params.copy()

        tf = str(self.params.get("timeframe", "M1"))
        symbol = str(self.params.get("symbol", ""))  # прокинут из MainWindow

        default_minutes = int(self.params.get("minutes", _minutes_from_timeframe(tf)))
        base_default = int(self.params.get("base_investment", 100))

        # Время экспирации
        self.minutes = QSpinBox()
        self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
        self.minutes.setValue(default_minutes)

        # Базовая «единица» (unit)
        self.base_investment = QSpinBox()
        self.base_investment.setRange(1, 50000)
        self.base_investment.setValue(base_default)


        # Ограничители
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 100)
        self.max_steps.setValue(self.params.get("max_steps", 20))

        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 1000)
        self.repeat_count.setValue(self.params.get("repeat_count", 10))

        self.min_balance = QSpinBox()
        self.min_balance.setRange(1, 10_000_000)
        self.min_balance.setValue(self.params.get("min_balance", 100))

        # Фильтр payout
        self.min_percent = QSpinBox()
        self.min_percent.setRange(0, 100)
        self.min_percent.setValue(self.params.get("min_percent", 70))

        # Повторный вход при поражении
        self.double_entry = QCheckBox()
        self.double_entry.setChecked(bool(self.params.get("double_entry", True)))
        double_entry_label = QLabel("Двойной вход на свечу")
        double_entry_label.mousePressEvent = lambda event: self.double_entry.toggle()

        self.parallel_trades = QCheckBox()
        self.parallel_trades.setChecked(
            bool(self.params.get("allow_parallel_trades", True))
        )
        parallel_label = QLabel("Обрабатывать множество сигналов")
        parallel_label.mousePressEvent = lambda event: self.parallel_trades.toggle()

        self.common_series = QCheckBox()
        self.common_series.setChecked(
            bool(self.params.get("use_common_series", False))
        )
        common_series_label = QLabel("Общая серия для всех сигналов")
        common_series_label.mousePressEvent = (
            lambda event: self.common_series.toggle()
        )

        form = QFormLayout()
        form.addRow("Базовая ставка (unit)", self.base_investment)
        form.addRow("Время экспирации (мин)", self.minutes)
        form.addRow("Макс. сделок в серии", self.max_steps)
        form.addRow("Повторов серии", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Мин. процент", self.min_percent)
        form.addRow(double_entry_label, self.double_entry)
        form.addRow(parallel_label, self.parallel_trades)
        form.addRow(common_series_label, self.common_series)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        form.addRow(btns)
        self.setLayout(form)

    def get_params(self) -> dict:
        # Мягкая нормализация минут через policy
        symbol = str(self.params.get("symbol", ""))
        m = int(self.minutes.value())
        norm = normalize_sprint(symbol, m)
        if norm is None:
            norm = 5 if symbol == "BTCUSDT" else (1 if m < 3 else max(3, min(500, m)))

        return {
            "minutes": int(norm),
            "base_investment": int(self.base_investment.value()),
            "max_steps": int(self.max_steps.value()),
            "repeat_count": int(self.repeat_count.value()),
            "min_balance": int(self.min_balance.value()),
            "min_percent": int(self.min_percent.value()),
            "double_entry": bool(self.double_entry.isChecked()),
            "allow_parallel_trades": bool(self.parallel_trades.isChecked()),
            "use_common_series": bool(self.common_series.isChecked()),
        }
