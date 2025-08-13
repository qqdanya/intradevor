# gui/bot_add_dialog.py
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QComboBox, QDialogButtonBox, QLabel


class AddBotDialog(QDialog):
    def __init__(
        self, available_symbols, available_strategies, strategy_labels=None, parent=None
    ):
        super().__init__(parent)

        self.setWindowTitle("Добавить нового бота")

        self.symbol = None
        self.strategy = None
        self.available_symbols = available_symbols
        self.available_strategies = available_strategies
        self.strategy_labels = strategy_labels or {}

        layout = QVBoxLayout()

        layout.addWidget(QLabel("Валютная пара:"))
        self.symbol_combo = QComboBox()
        self.symbol_combo.addItems(available_symbols)
        layout.addWidget(self.symbol_combo)

        layout.addWidget(QLabel("Алгоритм:"))
        self.strategy_combo = QComboBox()
        for key in self.available_strategies.keys():
            label = self.strategy_labels.get(key, key)
            self.strategy_combo.addItem(label, userData=key)
        layout.addWidget(self.strategy_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    @property
    def selected_symbol(self):
        return self.symbol_combo.currentText()

    @property
    def selected_strategy(self):
        return self.strategy_combo.currentData()

    def get_result(self):
        return self.symbol_combo.currentText(), self.strategy_combo.currentData()
