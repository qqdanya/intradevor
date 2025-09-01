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

# from core.ws_server import run_server


def run_gui():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme = config.get_theme()
    if qdarktheme and theme in ("light", "dark"):
        if hasattr(qdarktheme, "setup_theme"):
            qdarktheme.setup_theme(theme)
        elif hasattr(qdarktheme, "load_stylesheet"):
            app.setStyleSheet(qdarktheme.load_stylesheet(theme))
    else:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")
    font = QFont("Calibri", 10)
    app.setFont(font)

    # Устанавливаем шрифт из config, если задан
    font_family = config.get_font_family()
    font_size = config.get_font_size()
    if font_family or font_size:
        font = app.font()  # текущий системный шрифт
        if font_family:
            font.setFamily(font_family)
        if font_size:
            font.setPointSize(font_size)
        app.setFont(font)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    # asyncio.create_task(run_server())

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    run_gui()
