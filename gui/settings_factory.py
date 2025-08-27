from typing import Type, Dict
from PyQt6.QtWidgets import QDialog
from strategies.martingale import MartingaleStrategy
from strategies.oscar_grind_1 import OscarGrind1Strategy
from strategies.oscar_grind_2 import OscarGrind2Strategy
from gui.settings_martingale import MartingaleSettingsDialog
from gui.settings_oscar_grind import OscarGrindSettingsDialog

_registry: Dict[Type, Type[QDialog]] = {
    MartingaleStrategy: MartingaleSettingsDialog,
    OscarGrind1Strategy: OscarGrindSettingsDialog,
    OscarGrind2Strategy: OscarGrindSettingsDialog,
}


def get_settings_dialog_cls(strategy_cls: Type) -> Type[QDialog] | None:
    return _registry.get(strategy_cls)
