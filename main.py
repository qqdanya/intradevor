import sys
import asyncio

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from qasync import QEventLoop

try:
    import qdarktheme
except Exception:  # pragma: no cover - optional dependency
    qdarktheme = None

from gui.main_window import MainWindow
from core import config


def run_gui() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # ----------------------------
    # ТЕМА
    # ----------------------------
    theme = config.get_theme()
    if qdarktheme and theme in ("light", "dark"):
        if hasattr(qdarktheme, "setup_theme"):
            qdarktheme.setup_theme(theme)
        elif hasattr(qdarktheme, "load_stylesheet"):
            app.setStyleSheet(qdarktheme.load_stylesheet(theme))
    else:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")

    # ----------------------------
    # ШРИФТ (Calibri по умолчанию)
    # ----------------------------
    font = QFont("Calibri", 10)

    font_family = config.get_font_family()
    font_size = config.get_font_size()

    if font_family:
        font.setFamily(font_family)
    if font_size:
        font.setPointSize(font_size)

    app.setFont(font)

    # ----------------------------
    # ASYNC LOOP
    # ----------------------------
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # ----------------------------
    # GUI
    # ----------------------------
    window = MainWindow()
    window.show()

    # asyncio.create_task(run_server())

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    run_gui()
