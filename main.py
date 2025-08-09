import sys
from PyQt6.QtWidgets import QApplication
from qasync import QEventLoop
import asyncio
from gui.main_window import MainWindow
#from core.ws_server import run_server

def run_gui():
    app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    #asyncio.create_task(run_server())

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    run_gui()

