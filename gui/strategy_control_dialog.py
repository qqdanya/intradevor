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
from core.money import format_amount
from strategies.martingale import _minutes_from_timeframe
from core.policy import normalize_sprint
from core.money import format_money
from core.logger import ts


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
        self.lbl_timeframe = QLabel(bot.strategy_kwargs.get("timeframe", ""))
        for w in (self.lbl_strategy, self.lbl_symbol, self.lbl_timeframe):
            w.setStyleSheet("font-weight: 600;")
        hh.addWidget(QLabel("Стратегия:"))
        hh.addWidget(self.lbl_strategy)
        hh.addSpacing(12)
        hh.addWidget(QLabel("Символ:"))
        hh.addWidget(self.lbl_symbol)
        hh.addSpacing(12)
        hh.addWidget(QLabel("ТФ:"))
        hh.addWidget(self.lbl_timeframe)
        hh.addStretch(1)

        # ---------- ЛОГ (слева) ----------
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("Лог этой стратегии…")

        # История старых логов
        for line in self.main.bot_logs.get(self.bot, []):
            self.log_edit.append(line if str(line).startswith("[") else ts(str(line)))
        # Подписка на новые логи
        self._log_listener = lambda text: self.log_edit.append(
            text if str(text).startswith("[") else ts(str(text))
        )
        self.main.bot_log_listeners.setdefault(self.bot, []).append(self._log_listener)

        # ---------- ТАБЛИЦА СДЕЛОК (справа) ----------
        self.trades_table = QTableWidget(self)
        self.trades_table.setColumnCount(11)
        self.trades_table.setHorizontalHeaderLabels(
            [
                "Время сигнала",  # 0
                "Время ставки",  # 1
                "Пара",  # 2
                "ТФ",  # 3
                "Индикатор",  # 4  (если не прилетит — ставим "—")
                "Направление",  # 5
                "Ставка",  # 6
                "Время",  # 7
                "Процент",  # 8
                "P/L",  # 9
                "Счёт",  # 10
            ]
        )
        hdr = self.trades_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.setSortingEnabled(False)
        self.trades_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.trades_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.trades_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)

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

        strategy_key = str(self.bot.strategy_kwargs.get("strategy_key", "")).lower()
        self.strategy_key = strategy_key

        if strategy_key in ("oscar_grind_1", "oscar_grind_2"):
            self.minutes = QSpinBox()
            self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
            self.minutes.setValue(default_minutes)
            self.minutes.setToolTip("1; 3-500 мин; BTCUSDT: 5-500 мин")

            self.base_investment = QSpinBox()
            self.base_investment.setRange(1, 50_000)
            self.base_investment.setValue(int(getv("base_investment", 100)))

            tgt_default = int(getv("target_profit", getv("base_investment", 100)))
            self.target_profit = QSpinBox()
            self.target_profit.setRange(1, 1_000_000)
            self.target_profit.setValue(tgt_default)

            self.max_steps = QSpinBox()
            self.max_steps.setRange(1, 100)
            self.max_steps.setValue(int(getv("max_steps", 20)))

            self.repeat_count = QSpinBox()
            self.repeat_count.setRange(1, 1000)
            self.repeat_count.setValue(int(getv("repeat_count", 10)))

            self.min_balance = QSpinBox()
            self.min_balance.setRange(1, 10_000_000)
            self.min_balance.setValue(int(getv("min_balance", 100)))

            self.min_percent = QSpinBox()
            self.min_percent.setRange(0, 100)
            self.min_percent.setValue(int(getv("min_percent", 70)))

            self.lock_direction = QSpinBox()
            self.lock_direction.setRange(0, 1)
            self.lock_direction.setValue(1 if getv("lock_direction_to_first", True) else 0)

            form.addRow("Базовая ставка", self.base_investment)
            form.addRow("Цель серии, прибыль", self.target_profit)
            form.addRow("Время сделки (мин)", self.minutes)
            form.addRow("Макс. сделок в серии", self.max_steps)
            form.addRow("Повторов серии", self.repeat_count)
            form.addRow("Мин. баланс", self.min_balance)
            form.addRow("Мин. процент", self.min_percent)
            form.addRow("Фиксировать направление (0/1)", self.lock_direction)
        elif strategy_key == "fixed":
            self.minutes = QSpinBox()
            self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
            self.minutes.setValue(default_minutes)
            self.minutes.setToolTip("1; 3-500 мин; BTCUSDT: 5-500 мин")

            self.base_investment = QSpinBox()
            self.base_investment.setRange(1, 50_000)
            self.base_investment.setValue(int(getv("base_investment", 100)))

            self.repeat_count = QSpinBox()
            self.repeat_count.setRange(1, 1000)
            self.repeat_count.setValue(int(getv("repeat_count", 10)))

            self.min_balance = QSpinBox()
            self.min_balance.setRange(1, 10_000_000)
            self.min_balance.setValue(int(getv("min_balance", 100)))

            self.min_percent = QSpinBox()
            self.min_percent.setRange(0, 100)
            self.min_percent.setValue(int(getv("min_percent", 70)))

            form.addRow("Базовая ставка", self.base_investment)
            form.addRow("Время сделки (мин)", self.minutes)
            form.addRow("Количество ставок", self.repeat_count)
            form.addRow("Мин. баланс", self.min_balance)
            form.addRow("Мин. процент", self.min_percent)
        else:
            self.minutes = QSpinBox()
            self.minutes.setRange(5 if symbol == "BTCUSDT" else 1, 500)
            self.minutes.setValue(default_minutes)
            self.minutes.setToolTip("1; 3-500 мин; BTCUSDT: 5-500 мин")

            self.base_investment = QSpinBox()
            self.base_investment.setRange(1, 50_000)
            self.base_investment.setValue(int(getv("base_investment", 100)))
            self.max_steps = QSpinBox()
            self.max_steps.setRange(1, 20)
            default_max_steps = 3 if strategy_key == "antimartin" else 5
            self.max_steps.setValue(int(getv("max_steps", default_max_steps)))
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

            form.addRow("Базовая ставка", self.base_investment)
            form.addRow("Время сделки (мин)", self.minutes)
            form.addRow("Макс. шагов", self.max_steps)
            form.addRow("Повторов серии", self.repeat_count)
            form.addRow("Мин. баланс", self.min_balance)
            form.addRow("Коэффициент", self.coefficient)
            form.addRow("Мин. процент", self.min_percent)

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
        self.btn_toggle = QPushButton("🚀 Старт")
        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_delete = QPushButton("× Удалить")

        self.btn_toggle.clicked.connect(self._do_toggle)
        self.btn_stop.clicked.connect(self._do_stop)
        self.btn_delete.clicked.connect(self._do_delete)

        ch.addStretch(1)
        ch.addWidget(self.btn_toggle)
        ch.addWidget(self.btn_stop)
        ch.addWidget(self.btn_delete)

        # ---------- Главный блок: слева лог+настройки, справа таблица ----------
        left_panel = QWidget()
        lv = QVBoxLayout(left_panel)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        lv.addWidget(self.log_edit, 1)
        lv.addWidget(self.settings_box)
        lv.addWidget(settings_row)
        lv.addWidget(controls)

        top_split = QWidget()
        hs = QHBoxLayout(top_split)
        hs.setContentsMargins(0, 0, 0, 0)
        hs.setSpacing(8)
        hs.addWidget(left_panel, 1)
        hs.addWidget(self.trades_table, 1)

        # ---------- Layout ----------
        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(top_split, stretch=1)
        self.resize(1000, 600)

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
            self.btn_toggle.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        if not started:
            self.btn_toggle.setText("🚀 Старт")
        elif paused:
            self.btn_toggle.setText("▶ Продолжить")
        else:
            self.btn_toggle.setText("⏸ Пауза")

        self.btn_toggle.setEnabled(True)
        self.btn_stop.setEnabled(running)
        self.btn_delete.setEnabled(True)

    # ---- управление ----
    def _do_toggle(self):
        try:
            started = self.bot.has_started()
            st = self.bot.strategy
            paused = bool(st and hasattr(st, "is_paused") and st.is_paused())
            if not started:
                self.log_edit.clear()
                self.trades_table.setRowCount(0)
                self._pending_rows.clear()
                self.main.bot_logs[self.bot].clear()
                self.main.bot_trade_history[self.bot].clear()
                self.main.reset_bot(self.bot)
                self.bot.start()
                self.log_edit.append(ts("🚀 Старт стратегии."))
            elif paused:
                self.bot.resume()
                self.log_edit.append(ts("▶ Продолжено."))
            else:
                self.bot.pause()
                self.log_edit.append(ts("⏸ Пауза."))
        except Exception as e:
            self.log_edit.append(ts(f"⚠ Ошибка управления: {e}"))

    def _do_stop(self):
        try:
            self.bot.stop()
            self.log_edit.append(ts("⏹ Остановлено."))
            self.btn_toggle.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.btn_delete.setEnabled(True)
        except Exception as e:
            self.log_edit.append(ts(f"⚠ Ошибка остановки: {e}"))

    def _do_delete(self):
        try:
            self.main.delete_bot(self.bot)
            self.close()
        except Exception as e:
            self.log_edit.append(ts(f"⚠ Ошибка удаления: {e}"))

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

        if getattr(self, "strategy_key", "") in ("oscar_grind_1", "oscar_grind_2"):
            new_params = {
                "base_investment": self.base_investment.value(),
                "target_profit": self.target_profit.value(),
                "max_steps": self.max_steps.value(),
                "repeat_count": self.repeat_count.value(),
                "min_balance": self.min_balance.value(),
                "min_percent": self.min_percent.value(),
                "lock_direction_to_first": bool(self.lock_direction.value() == 1),
                "minutes": int(norm),
            }
        elif getattr(self, "strategy_key", "") == "fixed":
            new_params = {
                "base_investment": self.base_investment.value(),
                "repeat_count": self.repeat_count.value(),
                "min_balance": self.min_balance.value(),
                "min_percent": self.min_percent.value(),
                "minutes": int(norm),
            }
        else:
            new_params = {
                "base_investment": self.base_investment.value(),
                "max_steps": self.max_steps.value(),
                "repeat_count": self.repeat_count.value(),
                "min_balance": self.min_balance.value(),
                "coefficient": round(float(self.coefficient.value()), 2),
                "min_percent": self.min_percent.value(),
                "minutes": int(norm),
            }

        self.bot.strategy_kwargs.setdefault("params", {}).update(new_params)
        if self.bot.strategy and hasattr(self.bot.strategy, "update_params"):
            self.bot.strategy.update_params(**new_params)

        self.minutes.setValue(int(norm))

        formatted = []
        for k, v in new_params.items():
            if isinstance(v, float):
                formatted.append(f"'{k}': {format_amount(v)}")
            else:
                formatted.append(f"'{k}': {v}")
        self.log_edit.append(
            ts("💾 Настройки сохранены: {" + ", ".join(formatted) + "}")
        )

    # ---- хелперы: локальная таблица сделок ----
    def _fmt_money(self, value: float, ccy: str) -> str:
        # простенький формат (знаки +/−/— handled снаружи)
        try:
            v = float(value)
        except Exception:
            v = 0.0
        return format_money(v, ccy)

    def _add_trade_pending_local(
        self,
        *,
        trade_id: str,
        signal_at: str,
        placed_at: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        wait_seconds: float,
        account_mode: str | None = None,
        indicator: str | None = None,
        expected_end_ts: float | None = None,  # ⬅️ НОВОЕ: абсолютный дедлайн
    ):
        """
        Добавляем жёлтую строку с обратным отсчётом в ПРАВОЙ таблице диалога.
        Отсчёт синхронизирован по expected_end_ts, чтобы не «прыгало» при открытии окна.
        """
        from time import time as _now

        if expected_end_ts is None:
            expected_end_ts = _now() + float(wait_seconds)

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
        left_now = max(0.0, expected_end_ts - _now())
        account_txt = account_mode or (
            "ДЕМО" if getattr(self.main, "is_demo", False) else "РЕАЛ"
        )
        ccy = getattr(self.main, "account_currency", "RUB")
        ind_txt = indicator or "—"
        duration_txt = f"{int(round(float(wait_seconds) / 60))} мин"

        vals = [
            signal_at,  # 0 Время сигнала
            placed_at,  # 1 Время ставки
            symbol,  # 2 Пара
            timeframe,  # 3 ТФ
            ind_txt,  # 4 Индикатор
            dir_text,  # 5 Направление
            self._fmt_money(stake, ccy),  # 6 Ставка
            duration_txt,  # 7 Время
            f"{percent}%",  # 8 %
            f"Ожидание ({_fmt_left(left_now)})",  # 9 P/L
            account_txt,  # 10 Счёт
        ]
        for col, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            if col in (5, 9):
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.trades_table.setItem(row, col, it)

        yellow = QBrush(QColor("#fff4c2"))
        for c in range(self.trades_table.columnCount()):
            it = self.trades_table.item(row, c)
            if it:
                it.setBackground(yellow)

        timer = QTimer(self)
        timer.setInterval(1000)

        def _tick():
            left = expected_end_ts - _now()
            info = self._pending_rows.get(trade_id)
            if not info:
                timer.stop()
                return
            cur_row = info.get("row")
            if not isinstance(cur_row, int) or cur_row >= self.trades_table.rowCount():
                timer.stop()
                return
            item = self.trades_table.item(cur_row, 9)
            if item:
                item.setText(f"Ожидание ({_fmt_left(left)})")
            if left <= 0:
                timer.stop()

        timer.timeout.connect(_tick)
        timer.start()

        # сохраним pending, чтобы потом обновить по result
        prev = self._pending_rows.pop(trade_id, None)
        if prev and isinstance(prev.get("timer"), QTimer):
            try:
                prev["timer"].stop()
            except Exception:
                pass

        # сдвинем индексы ранее вставленных строк
        for info in self._pending_rows.values():
            r = info.get("row")
            if isinstance(r, int) and r >= row:
                info["row"] = r + 1

        self._pending_rows[trade_id] = {
            "row": row,
            "timer": timer,
            "expected_end_ts": float(expected_end_ts),
            "indicator": ind_txt,
            "signal_at": signal_at,
            "placed_at": placed_at,
            "wait_seconds": float(wait_seconds),
        }

        if was_sort:
            self.trades_table.setSortingEnabled(True)

    def _add_trade_result_local(
        self,
        *,
        trade_id: str | None = None,
        signal_at: str,
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
        ind_txt = indicator or "—"
        sig_time = signal_at
        place_time = placed_at
        duration_txt = ""
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
                ind_txt = info.get("indicator", ind_txt)
                sig_time = info.get("signal_at", sig_time)
                place_time = info.get("placed_at", place_time)
                duration_txt = f"{int(round(info.get('wait_seconds', 0.0) / 60))} мин"

        was_sort = self.trades_table.isSortingEnabled()
        if was_sort:
            self.trades_table.setSortingEnabled(False)

        if row_to_update is None:
            row_to_update = 0
            self.trades_table.insertRow(row_to_update)
            # сдвинем индексы pending'ов, т.к. вставили строку сверху
            for info in self._pending_rows.values():
                r = info.get("row")
                if isinstance(r, int) and r >= row_to_update:
                    info["row"] = r + 1

        dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
        account_txt = account_mode or (
            "ДЕМО" if getattr(self.main, "is_demo", False) else "РЕАЛ"
        )
        ccy = getattr(self.main, "account_currency", "RUB")

        vals = [
            sig_time,  # 0
            place_time,  # 1
            symbol,  # 2
            timeframe,  # 3
            ind_txt,  # 4
            dir_text,  # 5
            self._fmt_money(stake, ccy),  # 6
            duration_txt,  # 7
            f"{percent}%",  # 8
            fmt_pl(profit, ccy),  # 9
            account_txt,  # 10
        ]
        for col, v in enumerate(vals):
            item = QTableWidgetItem(str(v))
            if col in (5, 9):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.trades_table.setItem(row_to_update, col, item)

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
            self.log_edit.append(ts(f"⚠ Ошибка обновления таблицы сделок: {e}"))

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
