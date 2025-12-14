import sys
import asyncio
import logging
from typing import Optional
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont
from qasync import QEventLoop, asyncClose

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import qdarktheme
    QDARKTHEME_AVAILABLE = True
except ImportError:
    qdarktheme = None
    QDARKTHEME_AVAILABLE = False
    logger.info("qdarktheme not installed, using default theme")
except Exception as e:
    qdarktheme = None
    QDARKTHEME_AVAILABLE = False
    logger.warning(f"Failed to import qdarktheme: {e}")

from gui.main_window import MainWindow
from core import config


def setup_theme(app: QApplication) -> None:
    """Настройка темы приложения"""
    app.setStyle("Fusion")
    
    theme = config.get_theme()
    
    if QDARKTHEME_AVAILABLE and theme in ("light", "dark"):
        try:
            if hasattr(qdarktheme, "setup_theme"):
                qdarktheme.setup_theme(theme)
            elif hasattr(qdarktheme, "load_stylesheet"):
                app.setStyleSheet(qdarktheme.load_stylesheet(theme))
            logger.info(f"Applied qdarktheme: {theme}")
        except Exception as e:
            logger.error(f"Failed to apply qdarktheme: {e}")
            app.setPalette(app.style().standardPalette())
            app.setStyleSheet("")
    else:
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("")


def setup_font(app: QApplication) -> None:
    """Настройка шрифта приложения"""
    font_family = config.get_font_family()
    font_size = config.get_font_size()
    
    font = app.font()
    
    # Используем Calibri по умолчанию, если не задан в конфиге
    default_font = QFont("Calibri", 10)
    
    if font_family:
        font.setFamily(font_family)
    else:
        font.setFamily(default_font.family())
    
    if font_size:
        font.setPointSize(font_size)
    else:
        font.setPointSize(default_font.pointSize())
    
    app.setFont(font)
    logger.info(f"Font set: {font.family()}, size: {font.pointSize()}")


@asyncClose
async def main_async() -> None:
    """Основная асинхронная логика приложения"""
    # Запуск WebSocket сервера (если нужно)
    # server_task = asyncio.create_task(run_server())
    
    # Здесь можно добавить другие асинхронные задачи
    # await asyncio.gather(server_task)
    pass


def run_gui() -> int:
    """Запуск GUI приложения"""
    try:
        app = QApplication(sys.argv)
        
        # Настройка темы
        setup_theme(app)
        
        # Настройка шрифта
        setup_font(app)
        
        # Настройка асинхронного event loop
        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)
        
        # Создание и отображение главного окна
        window = MainWindow()
        window.show()
        
        # Запуск асинхронных задач
        asyncio.ensure_future(main_async())
        
        logger.info("Application started successfully")
        
        # Запуск основного цикла
        with loop:
            return loop.run_forever()
            
    except Exception as e:
        logger.critical(f"Failed to start application: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_gui())
