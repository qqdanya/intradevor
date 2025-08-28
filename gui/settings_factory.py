from typing import Type, Dict
from PyQt6.QtWidgets import QDialog
from strategies.martingale import MartingaleStrategy
from strategies.oscar_grind_1 import OscarGrind1Strategy
from strategies.oscar_grind_2 import OscarGrind2Strategy
from strategies.antimartin import AntiMartingaleStrategy
from strategies.fibonacci import FibonacciStrategy
from strategies.fixed import FixedStakeStrategy
from gui.settings_martingale import MartingaleSettingsDialog
from gui.settings_oscar_grind import OscarGrindSettingsDialog
from gui.settings_antimartin import AntimartinSettingsDialog
from gui.settings_fibonacci import FibonacciSettingsDialog
from gui.settings_fixed import FixedSettingsDialog

_registry: Dict[Type, Type[QDialog]] = {
    MartingaleStrategy: MartingaleSettingsDialog,
    OscarGrind1Strategy: OscarGrindSettingsDialog,
    OscarGrind2Strategy: OscarGrindSettingsDialog,
    AntiMartingaleStrategy: AntimartinSettingsDialog,
    FibonacciStrategy: FibonacciSettingsDialog,
    FixedStakeStrategy: FixedSettingsDialog,
}


def get_settings_dialog_cls(strategy_cls: Type) -> Type[QDialog] | None:
    return _registry.get(strategy_cls)
