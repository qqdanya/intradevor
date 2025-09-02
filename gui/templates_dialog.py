from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QListWidget,
    QPushButton,
    QWidget,
    QInputDialog,
)
from typing import List, Dict

from core.templates import load_templates, save_templates
from gui.settings_factory import get_settings_dialog_cls


class TemplatesDialog(QDialog):
    """Dialog for managing strategy templates."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main = main_window
        self.setWindowTitle("Шаблоны стратегий")

        layout = QVBoxLayout(self)

        self.strategy_combo = QComboBox(self)
        self.strategy_combo.addItems(list(self.main.strategy_labels.keys()))
        self.strategy_combo.currentTextChanged.connect(self._load_templates)
        layout.addWidget(self.strategy_combo)

        self.list_widget = QListWidget(self)
        layout.addWidget(self.list_widget, 1)

        btn_row = QWidget(self)
        bh = QHBoxLayout(btn_row)
        self.btn_add = QPushButton("Добавить", self)
        self.btn_rename = QPushButton("Переименовать", self)
        self.btn_delete = QPushButton("Удалить", self)
        self.btn_edit = QPushButton("Настройки", self)
        self.btn_up = QPushButton("Вверх", self)
        self.btn_down = QPushButton("Вниз", self)
        for b in (
            self.btn_add,
            self.btn_rename,
            self.btn_delete,
            self.btn_edit,
            self.btn_up,
            self.btn_down,
        ):
            bh.addWidget(b)
        layout.addWidget(btn_row)

        self.btn_add.clicked.connect(self._add_template)
        self.btn_rename.clicked.connect(self._rename_template)
        self.btn_delete.clicked.connect(self._delete_template)
        self.btn_edit.clicked.connect(self._edit_template)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)

        self.templates: List[Dict] = []
        self._load_templates(self.strategy_combo.currentText())

    # --- helpers ---
    def _current_strategy(self) -> str:
        return self.strategy_combo.currentText()

    def _load_templates(self, strategy_key: str) -> None:
        self.templates = load_templates(strategy_key)
        self.list_widget.clear()
        for tmpl in self.templates:
            self.list_widget.addItem(str(tmpl.get("name", "")))

    def _save(self) -> None:
        save_templates(self._current_strategy(), self.templates)

    def _default_name(self) -> str:
        return f"Шаблон {len(self.templates) + 1}"

    def _edit_params(self, params: Dict) -> Dict | None:
        """Open settings dialog for current strategy and return params or None."""
        strategy_key = self._current_strategy()
        strategy_cls = self.main.available_strategies.get(strategy_key)
        dlg_cls = get_settings_dialog_cls(strategy_cls) if strategy_cls else None
        if not dlg_cls:
            return params
        params = dict(params)
        params.setdefault("timeframe", "M1")
        params.setdefault("symbol", "")
        dlg = dlg_cls(params, parent=self)
        if dlg.exec():
            return dlg.get_params()
        return None

    def _add_template(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Новый шаблон", "Название:", text=self._default_name()
        )
        if ok and name:
            params = self._edit_params({})
            if params is None:
                return
            self.templates.append({"name": name, "params": params})
            self._save()
            self._load_templates(self._current_strategy())

    def _rename_template(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        tmpl = self.templates[row]
        name, ok = QInputDialog.getText(self, "Переименовать", "Новое название:", text=tmpl.get("name", ""))
        if ok and name:
            tmpl["name"] = name
            self._save()
            self._load_templates(self._current_strategy())
            self.list_widget.setCurrentRow(row)

    def _edit_template(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        tmpl = self.templates[row]
        params = tmpl.get("params", {})
        new_params = self._edit_params(params)
        if new_params is None:
            return
        tmpl["params"] = new_params
        self._save()
        self._load_templates(self._current_strategy())
        self.list_widget.setCurrentRow(row)

    def _delete_template(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        del self.templates[row]
        self._save()
        self._load_templates(self._current_strategy())

    def _move_up(self) -> None:
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        self.templates[row - 1], self.templates[row] = self.templates[row], self.templates[row - 1]
        self._save()
        self._load_templates(self._current_strategy())
        self.list_widget.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.templates) - 1:
            return
        self.templates[row + 1], self.templates[row] = self.templates[row], self.templates[row + 1]
        self._save()
        self._load_templates(self._current_strategy())
        self.list_widget.setCurrentRow(row + 1)
