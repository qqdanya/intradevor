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
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtCore import QTimer, Qt
from strategies.martingale import _minutes_from_timeframe
from core.policy import normalize_sprint


class StrategyControlDialog(QDialog):
    """
    Единое окно: статус + пер-ботовый лог + ВСТРОЕННЫЕ НАСТРОЙКИ + управление
    + СПРАВА таблица сделок этого бота.
    """

    def __init__(self, main_window, bot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Управление стратегией")
        self.main = main_window
        self.bot = bot

        # Локальный pending по этому диалогу (по trade_id)
        self._pending_rows: dict[str, dict] = {}

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

        # ---------- ЛОГ (слева) ----------
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("Лог этой стратегии…")

        # История старых логов
        for line in self.main.bot_logs.get(self.bot, []):
            self.log_edit.append(line)
        # Подписка на новые логи
        self._log_listener = lambda text: self.log_edit.append(text)
        self.main.bot_log_listeners.setdefault(self.bot, []).append(self._log_listener)

        # ---------- ТАБЛИЦА СДЕЛОК (справа) ----------
        self.trades_table = QTableWidget(self)
        self.trades_table.setColumnCount(9)
        self.trades_table.setHorizontalHeaderLabels(
            [
                "Время",  # 0
                "Пара",  # 1
                "ТФ",  # 2
                "Индикатор",  # 3  (если не прилетит — ставим "—")
                "Направление",  # 4
                "Ставка",  # 5
                "Процент",  # 6
                "P/L",  # 7
                "Счёт",  # 8
            ]
        )
        hdr = self.trades_table.horizontalHeader()
        # PyQt6: используем QHeaderView.ResizeMode.*
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # Время
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # Пара
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # ТФ
        hdr.setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )  # Индикатор
        hdr.setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )  # Направление
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # Ставка
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)  # %
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)  # P/L
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.ResizeToContents)  # Счёт
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.setSortingEnabled(True)

        # ---------- Настройки (inline) ----------
        self.settings_box = QGroupBox("Настройки стратегии")
        form = QFormLayout(self.settings_box)

        params = {}
        if self.bot.strategy and getattr(self.bot.strategy, "params", None):
            params = dict(self.bot.strategy.params)
        elif isinstance(self.bot.strategy_kwargs, dict):
            params = dict(self.bot.strategy_kwargs.get("params", {}) or {})

        def getv(key, default):
            return params.get(key, default)

        symbol = str(self.bot.strategy_kwargs.get("symbol", ""))
        tf = str(getv("timeframe", self.bot.strategy_kwargs.get("timeframe", "M1")))
        default_minutes = int(getv("minutes", _minutes_from_timeframe(tf)))

        self.minutes = QSpinBox()
        self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
        self.minutes.setValue(default_minutes)
        self.minutes.setToolTip("1; 3-500 мин; BTCUSDT: 5-500 мин")

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
        form.addRow("Время сделки (мин)", self.minutes)
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

        # ---------- Controls ----------
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

        # ---------- Верхний блок (лог слева + сделки справа) ----------
        top_split = QWidget()
        hs = QHBoxLayout(top_split)
        hs.setContentsMargins(0, 0, 0, 0)
        hs.setSpacing(8)
        hs.addWidget(self.log_edit, 1)
        hs.addWidget(self.trades_table, 1)

        # ---------- Layout ----------
        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(top_split, stretch=1)
        layout.addWidget(self.settings_box)
        layout.addWidget(settings_row)
        layout.addWidget(controls)

        # Таймер статуса/кнопок
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._refresh_status_and_buttons)
        self.timer.start()
        self._refresh_status_and_buttons()

        # === Подписка на сделки для КОНКРЕТНОГО бота ===
        # MainWindow будет вызывать наш колбэк, когда у ЭТОГО бота есть pending/result.
        self._trade_listener = self.handle_trade_event
        self.main.bot_trade_listeners.setdefault(self.bot, []).append(
            self._trade_listener
        )
        # ВОСПРОИЗВЕСТИ ИСТОРИЮ
        for kind, payload in self.main.bot_trade_history.get(self.bot, []):
            try:
                self.handle_trade_event(kind, payload)
            except Exception:
                pass

    # ---- обработка статуса/кнопок ----
    def _refresh_status_and_buttons(self):
        bots_now = []
        try:
            bots_now = list(self.main.bot_manager.get_all_bots())
        except Exception:
            pass
        bot_exists = self.bot in bots_now
        if not bot_exists:
            bot_exists = self.bot in getattr(self.main, "bot_items", {})

        started = bool(getattr(self.bot, "has_started", lambda: False)())
        running = self.bot.is_running()
        st = self.bot.strategy
        paused = bool(st and hasattr(st, "is_paused") and st.is_paused())

        if not bot_exists:
            self.lbl_status.setText("Статус: завершён / удалён")
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.btn_stop.setEnabled(False)
            return

        if not started and not st:
            status = "Статус: не запущено"
        elif running and paused:
            status = "Статус: пауза"
        elif running:
            status = "Статус: работает"
        else:
            status = "Статус: остановлено"
        self.lbl_status.setText(status)

        ccy = (
            st.params.get("account_currency")
            if (st and isinstance(getattr(st, "params", None), dict))
            else None
        )
        if ccy:
            self.lbl_ccy.setText(f"Валюта счёта: {ccy}")

        self.btn_start.setEnabled(bot_exists and not started)
        self.btn_pause.setEnabled(running and not paused)
        self.btn_resume.setEnabled(running and paused)
        self.btn_stop.setEnabled(running)

    # ---- управление ----
    def _do_start(self):
        try:
            if not self.bot.has_started():
                self.bot.start()
                self.log_edit.append("🚀 Старт стратегии.")
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
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_start.setEnabled(False)
        except Exception as e:
            self.log_edit.append(f"⚠ Ошибка остановки: {e}")

    # ---- сохранение настроек ----
    def save_settings(self):
        symbol = str(self.bot.strategy_kwargs.get("symbol", ""))
        m = int(self.minutes.value())
        norm = normalize_sprint(symbol, m)

        if norm is None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Недопустимое время экспирации")
            if symbol == "BTCUSDT":
                box.setText("Для BTCUSDT время должно быть от 5 до 500 минут.")
            else:
                box.setText(
                    "Для выбранной пары разрешено 1 или 3–500 минут (2 минуты — нельзя)."
                )
            box.open()
            return

        new_params = {
            "base_investment": self.base_investment.value(),
            "max_steps": self.max_steps.value(),
            "repeat_count": self.repeat_count.value(),
            "min_balance": self.min_balance.value(),
            "coefficient": self.coefficient.value(),
            "min_percent": self.min_percent.value(),
            "wait_on_low_percent": self.wait_on_low_percent.value(),
            "signal_timeout_sec": self.signal_timeout_sec.value(),
            "minutes": int(norm),
        }

        self.bot.strategy_kwargs.setdefault("params", {}).update(new_params)
        if self.bot.strategy and hasattr(self.bot.strategy, "update_params"):
            self.bot.strategy.update_params(**new_params)

        self.minutes.setValue(int(norm))
        self.log_edit.append(f"💾 Настройки сохранены: {new_params}")

    # ---- хелперы: локальная таблица сделок ----
    def _fmt_money(self, value: float, ccy: str) -> str:
        # простенький формат (знаки +/−/— handled снаружи)
        try:
            v = float(value)
        except Exception:
            v = 0.0
        return f"{v:.2f} {ccy}"

    def _add_trade_pending_local(
        self,
        *,
        trade_id: str,
        placed_at: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        wait_seconds: float,
        account_mode: str | None = None,
        indicator: str | None = None,
    ):
        """
        Добавляем жёлтую строку с обратным отсчётом в ПРАВОЙ таблице диалога.
        """
        from time import time as _now

        def _fmt_left(sec: float) -> str:
            s = int(max(0, round(sec)))
            h, r = divmod(s, 3600)
            m, s = divmod(r, 60)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            if m > 0:
                return f"{m}:{s:02d}"
            return f"{s} с"

        was_sort = self.trades_table.isSortingEnabled()
        if was_sort:
            self.trades_table.setSortingEnabled(False)

        row = 0
        self.trades_table.insertRow(row)

        dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
        remaining_txt = _fmt_left(wait_seconds)
        account_txt = account_mode or (
            "ДЕМО" if getattr(self.main, "is_demo", False) else "РЕАЛ"
        )
        ccy = getattr(self.main, "account_currency", "RUB")
        ind_txt = indicator or "—"

        vals = [
            placed_at,  # 0 Время
            symbol,  # 1 Пара
            timeframe,  # 2 ТФ
            ind_txt,  # 3 Индикатор
            dir_text,  # 4 Направление
            self._fmt_money(stake, ccy),  # 5 Ставка
            f"{percent}%",  # 6 %
            f"Ожидание ({remaining_txt})",  # 7 P/L
            account_txt,  # 8 Счёт
        ]
        for col, v in enumerate(vals):
            self.trades_table.setItem(row, col, QTableWidgetItem(str(v)))

        yellow = QBrush(QColor("#fff4c2"))
        for c in range(self.trades_table.columnCount()):
            it = self.trades_table.item(row, c)
            if it:
                it.setBackground(yellow)

        deadline = _now() + float(wait_seconds)
        timer = QTimer(self)
        timer.setInterval(1000)

        def _tick():
            left = deadline - _now()
            # строка могла уехать из‑за сортировки — трекать по индексу нельзя,
            # здесь для простоты держим “как есть”: верхняя строка в ожидании
            if row >= self.trades_table.rowCount():
                timer.stop()
                return
            item = self.trades_table.item(row, 7)
            if item:
                item.setText(f"Ожидание ({_fmt_left(left)})")
            if left <= 0:
                timer.stop()

        timer.timeout.connect(_tick)
        timer.start()

        # сохраним pending, чтобы обновить потом по trade_id
        prev = self._pending_rows.get(trade_id)
        if prev and isinstance(prev.get("timer"), QTimer):
            try:
                prev["timer"].stop()
            except Exception:
                pass
        self._pending_rows[trade_id] = {"row": row, "timer": timer}

        if was_sort:
            self.trades_table.setSortingEnabled(True)

    def _add_trade_result_local(
        self,
        *,
        trade_id: str | None = None,
        placed_at: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        profit: float | None,
        account_mode: str | None = None,
        indicator: str | None = None,
    ):
        """
        Обновляем/добавляем зелёную/красную/серую строку по результату.
        """

        def fmt_pl(p, ccy):
            if p is None:
                return "—"
            txt = self._fmt_money(p, ccy)
            return "+" + txt if p > 0 else txt

        # найти существующую строку по trade_id (если была pending)
        row_to_update = None
        if trade_id and trade_id in self._pending_rows:
            info = self._pending_rows.pop(trade_id, {})
            timer = info.get("timer")
            if isinstance(timer, QTimer):
                try:
                    timer.stop()
                except Exception:
                    pass
            row = info.get("row")
            if isinstance(row, int) and 0 <= row < self.trades_table.rowCount():
                row_to_update = row

        was_sort = self.trades_table.isSortingEnabled()
        if was_sort:
            self.trades_table.setSortingEnabled(False)

        if row_to_update is None:
            row_to_update = 0
            self.trades_table.insertRow(row_to_update)

        dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
        account_txt = account_mode or (
            "ДЕМО" if getattr(self.main, "is_demo", False) else "РЕАЛ"
        )
        ccy = getattr(self.main, "account_currency", "RUB")
        ind_txt = indicator or "—"

        vals = [
            placed_at,  # 0
            symbol,  # 1
            timeframe,  # 2
            ind_txt,  # 3
            dir_text,  # 4
            self._fmt_money(stake, ccy),  # 5
            f"{percent}%",  # 6
            fmt_pl(profit, ccy),  # 7
            account_txt,  # 8
        ]
        for col, v in enumerate(vals):
            self.trades_table.setItem(row_to_update, col, QTableWidgetItem(str(v)))

        if profit is None or abs(profit) < 1e-9:
            color = QColor("#e0e0e0")
        elif profit > 0:
            color = QColor("#d1f7c4")
        else:
            color = QColor("#ffd6d6")
        brush = QBrush(color)
        for c in range(self.trades_table.columnCount()):
            it = self.trades_table.item(row_to_update, c)
            if it:
                it.setBackground(brush)

        if was_sort:
            self.trades_table.setSortingEnabled(True)

    # ---- публичный колбэк для MainWindow ----
    def handle_trade_event(self, kind: str, payload: dict):
        """
        kind: "pending" | "result"
        payload: dict с полями, как у MainWindow.add_trade_pending/add_trade_result,
                 расширенно допускаем 'indicator'
        """
        try:
            if kind == "pending":
                self._add_trade_pending_local(**payload)
            else:
                self._add_trade_result_local(**payload)
        except Exception as e:
            # пусть ошибка в UI не роняет окно
            self.log_edit.append(f"⚠ Ошибка обновления таблицы сделок: {e}")

    # ---- жизнь/смерть окна ----
    def closeEvent(self, e):
        # убрать лог-листенер
        listeners = self.main.bot_log_listeners.get(self.bot, [])
        if self._log_listener in listeners:
            try:
                listeners.remove(self._log_listener)
            except Exception:
                pass

        # убрать подписку на сделки
        tlst = self.main.bot_trade_listeners.get(self.bot, [])
        if self._trade_listener in tlst:
            try:
                tlst.remove(self._trade_listener)
            except Exception:
                pass

        # остановить локальные таймеры ожиданий
        for info in list(self._pending_rows.values()):
            t = info.get("timer")
            if isinstance(t, QTimer):
                try:
                    t.stop()
                except Exception:
                    pass
        self._pending_rows.clear()

        super().closeEvent(e)
