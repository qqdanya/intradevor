# risk_dialog.py
from __future__ import annotations

from typing import Optional, Tuple
from PyQt6.QtWidgets import QDialog, QFormLayout, QSpinBox, QDialogButtonBox


class RiskDialog(QDialog):
    """
    Диалог установки дневных лимитов риска.
    - Минимальный баланс (%)  : 1..100
    - Максимальный баланс (%)  : 101..200
      При чтении значений из диалога (values / get_values) максимальный баланс
      возвращается как (введённое значение - 100). Пример: 150 -> 50.
    """

    def __init__(self, parent=None, min_default: int = 75, max_default: int = 200):
        super().__init__(parent)
        self.setWindowTitle("Ежедневное ограничение риска")

        # Минимальный баланс (%): 1..100
        self.min_spin = QSpinBox(self)
        self.min_spin.setRange(1, 100)
        self.min_spin.setValue(min_default)
        self.min_spin.setSuffix(" %")
        self.min_spin.setToolTip("Минимальный баланс (1–100%)")

        # Максимальный баланс (%): 101..200
        self.max_spin = QSpinBox(self)
        self.max_spin.setRange(101, 200)
        self.max_spin.setValue(max_default)
        self.max_spin.setSuffix(" %")
        self.max_spin.setToolTip(
            "Максимальный баланс (101–200%). "
            "В код будет передано значение на 100 меньше."
        )

        # Формa
        form = QFormLayout()
        form.addRow("Минимальный баланс (%):", self.min_spin)
        form.addRow("Максимальный баланс (%):", self.max_spin)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form.addRow(buttons)
        self.setLayout(form)
        self.adjustSize()

    def values(self) -> Tuple[int, int]:
        """
        Возвращает (min_percent, max_percent_adjusted),
        где max_percent_adjusted = отображаемое_значение - 100.
        Пример: при выборе 150 в диалоге вернёт 50.
        """
        min_val = self.min_spin.value()
        max_val_adjusted = self.max_spin.value() - 100
        return min_val, max_val_adjusted

    @staticmethod
    def get_values(
        parent=None, min_default: int = 1, max_default: int = 101
    ) -> Optional[Tuple[int, int]]:
        """
        Показывает диалог и возвращает (min_percent, max_percent_adjusted) или None при отмене.
        max_percent_adjusted = выбранный_в_диалоге - 100.
        """
        dlg = RiskDialog(parent, min_default=min_default, max_default=max_default)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.values()
        return None
