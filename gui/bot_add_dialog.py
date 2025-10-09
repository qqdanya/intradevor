from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
)
from PyQt6.QtCore import Qt

from gui.strategy_descriptions import STRATEGY_DESCRIPTIONS

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

        # Сначала выбор алгоритма
        layout.addWidget(QLabel("Алгоритм:"))
        algo_layout = QHBoxLayout()
        self.strategy_list = QListWidget()
        for key in self.available_strategies.keys():
            label = self.strategy_labels.get(key, key)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.strategy_list.addItem(item)
        algo_layout.addWidget(self.strategy_list)
        self.strategy_desc = QTextEdit()
        self.strategy_desc.setReadOnly(True)
        algo_layout.addWidget(self.strategy_desc)
        layout.addLayout(algo_layout)
        self.strategy_list.currentRowChanged.connect(self.on_strategy_change)
        self.strategy_list.setCurrentRow(0)

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
        self.tf_combo.setCurrentText(ALL_TF_LABEL)
        layout.addWidget(self.tf_combo)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)
        self.on_strategy_change()
        self.resize(self.sizeHint())

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
        # если есть совпадения кроме опции "Все валютные пары" — выбираем первое
        self.symbol_combo.setCurrentIndex(1 if len(filtered) > 1 else 0)
        self.on_strategy_change()

    def on_strategy_change(self, *_):
        key = self.selected_strategy
        desc = STRATEGY_DESCRIPTIONS.get(key, "")
        self.strategy_desc.setText(desc.strip())

    @property
    def selected_symbol(self):
        return self.symbol_combo.currentText()

    @property
    def selected_timeframe(self):
        return self.tf_combo.currentText()

    @property
    def selected_strategy(self):
        item = self.strategy_list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def get_result(self):
        return self.selected_symbol, self.selected_strategy

    def accept(self):
        super().accept()
