# risk_dialog.py
from __future__ import annotations

from typing import Optional, Tuple
from PyQt6.QtWidgets import QDialog, QFormLayout, QSpinBox, QDialogButtonBox


class RiskDialog(QDialog):
    """
    Диалог установки дневных лимитов риска.
    - risk_manage_min: 1..100 (%)
    - risk_manage_max: 1..500 (%)
    """

    def __init__(self, parent=None, min_default: int = 1, max_default: int = 100):
        super().__init__(parent)
        self.setWindowTitle("Ежедневное ограничение риска")

        self.min_spin = QSpinBox(self)
        self.min_spin.setRange(1, 100)
        self.min_spin.setValue(min_default)
        self.min_spin.setSuffix(" %")
        self.min_spin.setToolTip("Минимальный риск (1–100%)")

        self.max_spin = QSpinBox(self)
        self.max_spin.setRange(1, 500)
        self.max_spin.setValue(max_default)
        self.max_spin.setSuffix(" %")
        self.max_spin.setToolTip("Максимальный риск (до 500%)")

        form = QFormLayout()
        form.addRow("Минимум:", self.min_spin)
        form.addRow("Максимум:", self.max_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form.addRow(buttons)
        self.setLayout(form)

    def values(self) -> Tuple[int, int]:
        return self.min_spin.value(), self.max_spin.value()

    @staticmethod
    def get_values(
        parent=None, min_default: int = 1, max_default: int = 100
    ) -> Optional[Tuple[int, int]]:
        """
        Удобный статический метод: показывает диалог и возвращает (min, max) или None при отмене.
        """
        dlg = RiskDialog(parent, min_default=min_default, max_default=max_default)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.values()
        return None
