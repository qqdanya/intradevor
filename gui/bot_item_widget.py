from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

class BotItemWidget(QWidget):
    def __init__(self, title: str, on_settings, on_pause_resume, on_stop, parent=None):
        super().__init__(parent)
        self._paused = False

        self.label = QLabel(title)
        self.btn_settings = QPushButton("⚙")
        self.btn_pause = QPushButton("⏸")
        self.btn_stop = QPushButton("⏹")

        self.btn_settings.clicked.connect(on_settings)
        self.btn_pause.clicked.connect(self._toggle)
        self.btn_stop.clicked.connect(on_stop)

        layout = QHBoxLayout(self)
        layout.addWidget(self.label)
        layout.addStretch(1)
        layout.addWidget(self.btn_settings)
        layout.addWidget(self.btn_pause)
        layout.addWidget(self.btn_stop)

        # внешние колбэки
        self._on_pause_resume = on_pause_resume

    def _toggle(self):
        self._paused = not self._paused
        self.btn_pause.setText("▶" if self._paused else "⏸")
        if self._on_pause_resume:
            self._on_pause_resume(self._paused)

    def set_paused(self, paused: bool):
        if self._paused != paused:
            self._toggle()

