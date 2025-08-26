from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
)

ALL_SYMBOLS_LABEL = "Все валютные пары"
ALL_TF_LABEL = "Все таймфреймы"

TIMEFRAMES = [ALL_TF_LABEL, "M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]


class AddBotDialog(QDialog):
    def __init__(
        self, available_symbols, available_strategies, strategy_labels=None, parent=None
    ):
        super().__init__(parent)

        self.setWindowTitle("Добавить нового бота")

        # дополняем список символов опцией "Все валютные пары"
        self.available_symbols = [ALL_SYMBOLS_LABEL] + list(available_symbols)
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
        self.symbol_combo.addItems(self.available_symbols)
        layout.addWidget(self.symbol_combo)

        layout.addWidget(QLabel("Таймфрейм:"))
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
        self.strategy_combo.currentIndexChanged.connect(self.on_strategy_change)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)
        self.on_strategy_change()

    def filter_symbols(self, text: str):
        self.symbol_combo.clear()
        t = text.upper().replace("/", "")
        filtered = []

        # Всегда держим опцию "Все валютные пары" сверху
        filtered.append(ALL_SYMBOLS_LABEL)

        for s in self.available_symbols:
            if s == ALL_SYMBOLS_LABEL:
                continue
            s_no_slash = s.replace("/", "")
            if t in s.upper() or t in s_no_slash:
                filtered.append(s)
        self.symbol_combo.addItems(filtered)
        self.on_strategy_change()

    def on_strategy_change(self, *_):
        key = self.selected_strategy
        is_martingale = key == "martingale"

        # disable "all" options for Martingale
        sym_item = self.symbol_combo.model().item(0)
        tf_item = self.tf_combo.model().item(0)
        if sym_item:
            sym_item.setEnabled(not is_martingale)
        if tf_item:
            tf_item.setEnabled(not is_martingale)

        if is_martingale:
            if self.symbol_combo.currentIndex() == 0 and self.symbol_combo.count() > 1:
                self.symbol_combo.setCurrentIndex(1)
            if self.tf_combo.currentIndex() == 0 and self.tf_combo.count() > 1:
                self.tf_combo.setCurrentIndex(1)

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

    def accept(self):
        if (
            self.selected_strategy == "martingale"
            and (
                self.selected_symbol == ALL_SYMBOLS_LABEL
                or self.selected_timeframe == ALL_TF_LABEL
            )
        ):
            QMessageBox.warning(
                self,
                "Ошибка",
                "Стратегия 'Мартингейл' работает только с одной валютной парой и таймфреймом.",
            )
            return
        super().accept()
