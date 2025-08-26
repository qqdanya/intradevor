from typing import Type, Dict
from PyQt6.QtWidgets import QDialog
from strategies.martingale import MartingaleStrategy
from strategies.oscar_grind import OscarGrindStrategy
from gui.settings_martingale import MartingaleSettingsDialog
from gui.settings_oscar_grind import OscarGrindSettingsDialog

_registry: Dict[Type, Type[QDialog]] = {
    MartingaleStrategy: MartingaleSettingsDialog,
    OscarGrindStrategy: OscarGrindSettingsDialog,
}


def get_settings_dialog_cls(strategy_cls: Type) -> Type[QDialog] | None:
    return _registry.get(strategy_cls)
