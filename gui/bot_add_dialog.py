from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
)

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]


class AddBotDialog(QDialog):
    def __init__(
        self, available_symbols, available_strategies, strategy_labels=None, parent=None
    ):
        super().__init__(parent)

        self.setWindowTitle("Добавить нового бота")

        self.available_symbols = available_symbols
        self.available_strategies = available_strategies
        self.strategy_labels = strategy_labels or {}

        layout = QVBoxLayout()

        # Поиск валют
        layout.addWidget(QLabel("Поиск валютной пары:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Например: EUR, USD...")
        self.search_edit.textChanged.connect(self.filter_symbols)
        layout.addWidget(self.search_edit)

        # Список валют
        layout.addWidget(QLabel("Валютная пара:"))
        self.symbol_combo = QComboBox()
        self.symbol_combo.addItems(available_symbols)
        layout.addWidget(self.symbol_combo)

        layout.addWidget(QLabel("Таймфрейм:"))  # <<< новое поле
        self.tf_combo = QComboBox()
        self.tf_combo.addItems(TIMEFRAMES)
        self.tf_combo.setCurrentText("M1")
        layout.addWidget(self.tf_combo)

        # Стратегии
        layout.addWidget(QLabel("Алгоритм:"))
        self.strategy_combo = QComboBox()
        for key in self.available_strategies.keys():
            label = self.strategy_labels.get(key, key)
            self.strategy_combo.addItem(label, userData=key)
        layout.addWidget(self.strategy_combo)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def filter_symbols(self, text):
        self.symbol_combo.clear()
        filtered = [s for s in self.available_symbols if text.upper() in s.upper()]
        self.symbol_combo.addItems(filtered)

    @property
    def selected_symbol(self):
        return self.symbol_combo.currentText()

    @property
    def selected_timeframe(self):
        return self.tf_combo.currentText()

    @property
    def selected_strategy(self):
        return self.strategy_combo.currentData()

    def get_result(self):
        return self.selected_symbol, self.selected_strategy
