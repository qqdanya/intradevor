from typing import Type, Dict
from PyQt6.QtWidgets import QDialog
from strategies.martingale import MartingaleStrategy
from gui.settings_martingale import MartingaleSettingsDialog

_registry: Dict[Type, Type[QDialog]] = {
    MartingaleStrategy: MartingaleSettingsDialog,
    # позже добавишь: OscarGrindStrategy: OscarGrindSettingsDialog
}


def get_settings_dialog_cls(strategy_cls: Type) -> Type[QDialog] | None:
    return _registry.get(strategy_cls)
