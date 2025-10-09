from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
    QInputDialog,
    QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from typing import List, Dict

from core.templates import load_templates, save_templates
from gui.settings_factory import get_settings_dialog_cls


class TemplatesDialog(QDialog):
    """Dialog for managing strategy templates."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main = main_window
        self.setWindowTitle("Шаблоны стратегий")

        layout = QHBoxLayout(self)

        # список стратегий слева
        self.strategy_list = QListWidget(self)
        for key, label in self.main.strategy_labels.items():
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.strategy_list.addItem(item)
        self.strategy_list.currentRowChanged.connect(self._on_strategy_change)
        layout.addWidget(self.strategy_list)

        # средняя колонка: кнопки + шаблоны
        tmpl_column = QVBoxLayout()

        btn_row = QWidget(self)
        bh = QHBoxLayout(btn_row)
        self.btn_add = QPushButton("Добавить", self)
        self.btn_rename = QPushButton("Переименовать", self)
        self.btn_save = QPushButton("Сохранить", self)
        self.btn_delete = QPushButton("Удалить", self)
        self.btn_up = QPushButton("Вверх", self)
        self.btn_down = QPushButton("Вниз", self)
        for b in (
            self.btn_add,
            self.btn_rename,
            self.btn_save,
            self.btn_delete,
            self.btn_up,
            self.btn_down,
        ):
            bh.addWidget(b)
        tmpl_column.addWidget(btn_row)

        self.list_widget = QListWidget(self)
        self.list_widget.currentRowChanged.connect(self._show_template_settings)
        tmpl_column.addWidget(self.list_widget, 1)
        layout.addLayout(tmpl_column)

        # правая область: настройки выбранного шаблона
        self.settings_container = QWidget(self)
        self.settings_layout = QVBoxLayout(self.settings_container)
        layout.addWidget(self.settings_container, 1)

        self.btn_add.clicked.connect(self._add_template)
        self.btn_rename.clicked.connect(self._rename_template)
        self.btn_delete.clicked.connect(self._delete_template)
        self.btn_save.clicked.connect(self._save_current_settings)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)

        self.templates: List[Dict] = []
        self.current_settings_widget = None
        self.strategy_list.setCurrentRow(0)

    # --- helpers ---
    def _current_strategy(self) -> str:
        item = self.strategy_list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return ""

    def _on_strategy_change(self, *_):
        self._load_templates(self._current_strategy())

    def _load_templates(self, strategy_key: str) -> None:
        self.templates = load_templates(strategy_key)
        self.list_widget.clear()
        for tmpl in self.templates:
            self.list_widget.addItem(str(tmpl.get("name", "")))
        if self.templates:
            self.list_widget.setCurrentRow(0)
        else:
            self._show_template_settings(-1)

    def _save(self) -> None:
        save_templates(self._current_strategy(), self.templates)

    def _default_name(self) -> str:
        return f"Шаблон {len(self.templates) + 1}"

    def _show_template_settings(self, row: int) -> None:
        while self.settings_layout.count():
            item = self.settings_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.current_settings_widget = None
        if row < 0 or row >= len(self.templates):
            return
        tmpl = self.templates[row]
        params = tmpl.get("params", {})
        strategy_key = self._current_strategy()
        strategy_cls = self.main.available_strategies.get(strategy_key)
        dlg_cls = get_settings_dialog_cls(strategy_cls) if strategy_cls else None
        if not dlg_cls:
            return
        params = dict(params)
        params.setdefault("timeframe", "M1")
        params.setdefault("symbol", "")
        widget = dlg_cls(params, parent=self)
        btn_box = widget.findChild(QDialogButtonBox)
        if btn_box:
            btn_box.setVisible(False)
        self.settings_layout.addWidget(widget)
        self.current_settings_widget = widget

    def _save_current_settings(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or self.current_settings_widget is None:
            return
        params = self.current_settings_widget.get_params()
        self.templates[row]["params"] = params
        self._save()

    def _add_template(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Новый шаблон", "Название:", text=self._default_name()
        )
        if ok and name:
            self.templates.append({"name": name, "params": {}})
            self._save()
            self._load_templates(self._current_strategy())
            self.list_widget.setCurrentRow(len(self.templates) - 1)

    def _rename_template(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        tmpl = self.templates[row]
        name, ok = QInputDialog.getText(
            self, "Переименовать", "Новое название:", text=tmpl.get("name", "")
        )
        if ok and name:
            tmpl["name"] = name
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
        self.templates[row - 1], self.templates[row] = (
            self.templates[row],
            self.templates[row - 1],
        )
        self._save()
        self._load_templates(self._current_strategy())
        self.list_widget.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.templates) - 1:
            return
        self.templates[row + 1], self.templates[row] = (
            self.templates[row],
            self.templates[row + 1],
        )
        self._save()
        self._load_templates(self._current_strategy())
        self.list_widget.setCurrentRow(row + 1)

