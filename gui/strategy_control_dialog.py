# gui/strategy_control_dialog.py
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QWidget,
    QGroupBox,
    QFormLayout,
    QSpinBox,
    QDoubleSpinBox,
)
from PyQt6.QtCore import QTimer


class StrategyControlDialog(QDialog):
    """
    Единое окно: статус + пер‑ботовый лог + ВСТРОЕННЫЕ НАСТРОЙКИ + управление (Старт/Пауза/Продолжить/Стоп).
    """

    def __init__(self, main_window, bot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Управление стратегией")
        self.main = main_window
        self.bot = bot

        # ---------- Header ----------
        header = QWidget()
        hh = QHBoxLayout(header)
        self.lbl_strategy = QLabel(
            self.main.strategy_label(bot.strategy_kwargs.get("strategy_key", ""))
        )
        self.lbl_symbol = QLabel(bot.strategy_kwargs.get("symbol", ""))
        self.lbl_status = QLabel("Статус: —")
        self.lbl_ccy = QLabel("Валюта счёта: —")
        for w in (self.lbl_strategy, self.lbl_symbol, self.lbl_status, self.lbl_ccy):
            w.setStyleSheet("font-weight: 600;")
        hh.addWidget(QLabel("Стратегия:"))
        hh.addWidget(self.lbl_strategy)
        hh.addSpacing(12)
        hh.addWidget(QLabel("Символ:"))
        hh.addWidget(self.lbl_symbol)
        hh.addStretch(1)
        hh.addWidget(self.lbl_status)
        hh.addSpacing(12)
        hh.addWidget(self.lbl_ccy)

        # ---------- Log ----------
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("Лог этой стратегии…")

        # История
        for line in self.main.bot_logs.get(self.bot, []):
            self.log_edit.append(line)
        # Подписка
        self._listener = lambda text: self.log_edit.append(text)
        self.main.bot_log_listeners.setdefault(self.bot, []).append(self._listener)

        # ---------- Settings (inline) ----------
        self.settings_box = QGroupBox("Настройки стратегии")
        form = QFormLayout(self.settings_box)

        params = {}
        if self.bot.strategy and getattr(self.bot.strategy, "params", None):
            params = dict(self.bot.strategy.params)
        elif isinstance(self.bot.strategy_kwargs, dict):
            params = dict(self.bot.strategy_kwargs.get("params", {}) or {})

        def getv(key, default):
            return params.get(key, default)

        self.base_investment = QSpinBox()
        self.base_investment.setRange(1, 50_000)
        self.base_investment.setValue(int(getv("base_investment", 100)))
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 20)
        self.max_steps.setValue(int(getv("max_steps", 5)))
        self.repeat_count = QSpinBox()
        self.repeat_count.setRange(1, 1000)
        self.repeat_count.setValue(int(getv("repeat_count", 10)))
        self.min_balance = QSpinBox()
        self.min_balance.setRange(1, 10_000_000)
        self.min_balance.setValue(int(getv("min_balance", 100)))
        self.coefficient = QDoubleSpinBox()
        self.coefficient.setRange(1.0, 10.0)
        self.coefficient.setSingleStep(0.1)
        self.coefficient.setValue(float(getv("coefficient", 2.0)))
        self.min_percent = QSpinBox()
        self.min_percent.setRange(0, 100)
        self.min_percent.setValue(int(getv("min_percent", 70)))
        self.wait_on_low_percent = QSpinBox()
        self.wait_on_low_percent.setRange(0, 60)
        self.wait_on_low_percent.setValue(int(getv("wait_on_low_percent", 1)))
        self.signal_timeout_sec = QSpinBox()
        self.signal_timeout_sec.setRange(1, 24 * 3600)
        self.signal_timeout_sec.setValue(int(getv("signal_timeout_sec", 3600)))

        form.addRow("Базовая ставка", self.base_investment)
        form.addRow("Макс. шагов", self.max_steps)
        form.addRow("Повторов серии", self.repeat_count)
        form.addRow("Мин. баланс", self.min_balance)
        form.addRow("Коэффициент", self.coefficient)
        form.addRow("Мин. процент", self.min_percent)
        form.addRow("Ожидание при низком % (с)", self.wait_on_low_percent)
        form.addRow("Таймаут ожидания сигнала (с)", self.signal_timeout_sec)

        # Кнопка «Сохранить настройки»
        settings_row = QWidget()
        sh = QHBoxLayout(settings_row)
        self.btn_save_settings = QPushButton("💾 Сохранить настройки")
        self.btn_save_settings.clicked.connect(self.save_settings)
        sh.addStretch(1)
        sh.addWidget(self.btn_save_settings)

        # ---------- Controls: start/pause/resume/stop ----------
        controls = QWidget()
        ch = QHBoxLayout(controls)
        self.btn_start = QPushButton("🚀 Старт")
        self.btn_pause = QPushButton("⏸ Пауза")
        self.btn_resume = QPushButton("▶ Продолжить")
        self.btn_stop = QPushButton("⏹ Стоп")

        self.btn_start.clicked.connect(self._do_start)
        self.btn_pause.clicked.connect(self._do_pause)
        self.btn_resume.clicked.connect(self._do_resume)
        self.btn_stop.clicked.connect(self._do_stop)

        ch.addStretch(1)
        ch.addWidget(self.btn_start)
        ch.addWidget(self.btn_pause)
        ch.addWidget(self.btn_resume)
        ch.addWidget(self.btn_stop)

        # ---------- Layout ----------
        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.log_edit, stretch=1)
        layout.addWidget(self.settings_box)
        layout.addWidget(settings_row)
        layout.addWidget(controls)

        # Таймер статуса/кнопок
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._refresh_status_and_buttons)
        self.timer.start()
        self._refresh_status_and_buttons()

    # ---- actions ----
    def _refresh_status_and_buttons(self):
        # Бот всё ещё существует в UI/менеджере?
        bot_exists = self.bot in getattr(self.main, "bot_items", {})
        # или так, если предпочитаешь менеджер:
        # bot_exists = self.bot in self.main.bot_manager.get_all_bots()

        started = bool(getattr(self.bot, "has_started", lambda: False)())
        running = self.bot.is_running()
        st = self.bot.strategy
        paused = bool(st and hasattr(st, "is_paused") and st.is_paused())

        if not bot_exists:
            # Бота уже нет: показываем финальный статус и блокируем управление
            self.lbl_status.setText("Статус: завершён / удалён")
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.btn_stop.setEnabled(False)
            # опционально: закрыть окно автоматически
            # self.accept()
            return

        # обычные состояния, пока бот жив
        if not started and not st:
            status = "Статус: не запущено"
        elif running and paused:
            status = "Статус: пауза"
        elif running:
            status = "Статус: работает"
        else:
            status = "Статус: остановлено"
        self.lbl_status.setText(status)

        # валюта, если известна
        ccy = (
            st.params.get("account_currency")
            if (st and isinstance(getattr(st, "params", None), dict))
            else None
        )
        if ccy:
            self.lbl_ccy.setText(f"Валюта счёта: {ccy}")

        # доступность кнопок:
        # «Старт» разрешаем только если бот ещё существует И ещё не запускался
        self.btn_start.setEnabled(bot_exists and not started)
        self.btn_pause.setEnabled(running and not paused)
        self.btn_resume.setEnabled(running and paused)
        self.btn_stop.setEnabled(running)

    def _do_start(self):
        try:
            if not self.bot.has_started():
                self.bot.start()
                self.log_edit.append("🚀 Старт стратегии.")
                # В MainWindow у тебя логируется запуск — тут добавим локально для наглядности
        except Exception as e:
            self.log_edit.append(f"⚠ Ошибка старта: {e}")

    def _do_pause(self):
        try:
            self.bot.pause()
            self.log_edit.append("⏸ Пауза.")
        except Exception as e:
            self.log_edit.append(f"⚠ Ошибка паузы: {e}")

    def _do_resume(self):
        try:
            self.bot.resume()
            self.log_edit.append("▶ Продолжено.")
        except Exception as e:
            self.log_edit.append(f"⚠ Ошибка продолжения: {e}")

    def _do_stop(self):
        try:
            self.bot.stop()
            self.log_edit.append("⏹ Остановлено.")
            # сразу выключим кнопки на всякий случай; on_finish удалит бота,
            # и через таймер _refresh_status_and_buttons всё обновится окончательно
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_start.setEnabled(False)
        except Exception as e:
            self.log_edit.append(f"⚠ Ошибка остановки: {e}")

    def save_settings(self):
        new_params = {
            "base_investment": self.base_investment.value(),
            "max_steps": self.max_steps.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "coefficient": self.coefficient.value(),
            "min_percent": self.min_percent.value(),
            "wait_on_low_percent": self.wait_on_low_percent.value(),
            "signal_timeout_sec": self.signal_timeout_sec.value(),
        }
        # 1) дефолты для будущих перезапусков
        self.bot.strategy_kwargs.setdefault("params", {}).update(new_params)
        # 2) на лету — если уже запущено
        if self.bot.strategy and hasattr(self.bot.strategy, "update_params"):
            self.bot.strategy.update_params(**new_params)
        self.log_edit.append(f"💾 Настройки сохранены: {new_params}")

    def closeEvent(self, e):
        listeners = self.main.bot_log_listeners.get(self.bot, [])
        if self._listener in listeners:
            listeners.remove(self._listener)
        super().closeEvent(e)
