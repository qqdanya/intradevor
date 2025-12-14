# gui/main_window.py
from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QMenuBar,
    QApplication,
    QFontDialog,
)
from PyQt6.QtGui import QTextCursor, QColor, QFont, QMovie
from PyQt6.QtCore import Qt, QTimer, QSize
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from functools import partial
import asyncio
try:
    import qdarktheme
except Exception:  # pragma: no cover - optional dependency
    qdarktheme = None

from core.money import format_money
from core.logger import ts
from core import config

from gui.bot_add_dialog import AddBotDialog, ALL_SYMBOLS_LABEL
from gui.risk_dialog import RiskDialog
from gui.trades_table_widget import TradesTableWidget
from gui.templates_dialog import TemplatesDialog
from core.session import (
    create_http_client_from_browser_cookies,
    refresh_http_client_cookies,
    extract_user_credentials_from_client,
)
from core.intrade_api_async import (
    get_balance_info,
    change_currency,
    is_demo_account,
    toggle_real_demo,
    set_risk,
)
from core.ws_client import listen_to_signals
from core.bot_manager import BotManager
from core.bot import Bot
from strategies.martingale import MartingaleStrategy
from strategies.oscar_grind_1 import OscarGrind1Strategy
from strategies.oscar_grind_2 import OscarGrind2Strategy
from strategies.antimartin import AntiMartingaleStrategy
from strategies.fibonacci import FibonacciStrategy
from strategies.fixed import FixedStakeStrategy


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        # === Меню ===
        self.menu_bar = QMenuBar(self)
        font_menu = self.menu_bar.addMenu("Шрифт")
        font_menu.addAction("Выбрать...", self._choose_font)
        theme_menu = self.menu_bar.addMenu("Тема")
        if qdarktheme:
            def _apply_theme(mode: str) -> None:
                app = QApplication.instance()
                if mode in ("light", "dark"):
                    if hasattr(qdarktheme, "setup_theme"):
                        qdarktheme.setup_theme(mode)
                    elif hasattr(qdarktheme, "load_stylesheet"):
                        app.setStyleSheet(qdarktheme.load_stylesheet(mode))
                else:
                    app.setPalette(app.style().standardPalette())
                    app.setStyleSheet("")
                config.set_theme(mode)
                config.save_config()

            theme_menu.addAction("Светлая", lambda: _apply_theme("light"))
            theme_menu.addAction("Тёмная", lambda: _apply_theme("dark"))
            theme_menu.addAction("Системная", lambda: _apply_theme("system"))
        else:
            theme_menu.setEnabled(False)

        # Управление шаблонами стратегий
        self.menu_bar.addAction("Шаблоны стратегий", self._open_templates_dialog)

        # === имя/версия приложения ===
        try:
            from core.config import APP_NAME as _APP_NAME, APP_VERSION as _APP_VERSION
        except Exception:
            _APP_NAME, _APP_VERSION = "Intradevor", "0.0.0"

        self.http_client = None
        self.user_id = None
        self.user_hash = None
        self.account_currency = "RUB"
        self.is_demo = False

        self.available_symbols = [
            "AUD/CAD",
            "AUD/CHF",
            "AUD/JPY",
            "AUD/NZD",
            "AUD/USD",
            "CAD/JPY",
            "EUR/AUD",
            "EUR/CAD",
            "EUR/CHF",
            "EUR/GBP",
            "EUR/JPY",
            "EUR/USD",
            "GBP/AUD",
            "GBP/CHF",
            "GBP/JPY",
            "GBP/NZD",
            "NZD/JPY",
            "NZD/USD",
            "USD/CAD",
            "USD/CHF",
            "USD/JPY",
            "BTC/USDT",
        ]
        self.available_strategies = {
            "martingale": MartingaleStrategy,
            "antimartin": AntiMartingaleStrategy,
            "oscar_grind_1": OscarGrind1Strategy,
            "oscar_grind_2": OscarGrind2Strategy,
            "fibonacci": FibonacciStrategy,
            "fixed": FixedStakeStrategy,
        }
        self.strategy_labels = {
            "martingale": "Мартингейл",
            "antimartin": "Антимартин",
            "oscar_grind_1": "Оскар Грайнд 1",
            "oscar_grind_2": "Оскар Грайнд 2",
            "fibonacci": "Фибоначчи",
            "fixed": "Фиксированная ставка",
        }

        self.bot_ever_started = defaultdict(bool)
        self.bot_logs = defaultdict(list)
        self.bot_log_listeners = defaultdict(list)
        self.bot_trade_listeners = defaultdict(list)
        self.bot_trade_history = defaultdict(list)
        self.bot_pending_trades = defaultdict(set)
        self.bot_last_phase: dict[Bot, str] = {}
        self.strategy_windows: dict[Bot, "StrategyControlDialog"] = {}

        self.user_id_label = QLabel("user_id: loading...")
        self.user_hash_label = QLabel("user_hash: loading...")
        self.balance_label = QLabel("Баланс: loading...")
        self.moscow_time_label = QLabel("--:--:--")
        self.moscow_time_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        time_font = QFont("Monospace")
        time_font.setStyleHint(QFont.StyleHint.TypeWriter)
        time_font.setPixelSize(40)
        self.moscow_time_label.setFont(time_font)
        self.moscow_time_label.setStyleSheet(
            "font-weight: 400; letter-spacing: 1px;"
        )

        self.time_gif_label = QLabel()
        self.time_gif_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.time_gif_label.setFixedSize(1200, 120)
        self.time_gif_label.setStyleSheet("padding-top: 8px;")
        self.time_gif_movie: QMovie | None = None

        # Сделаем лейблы фиксированными по вертикали, чтобы их не растягивало
        from PyQt6.QtWidgets import QSizePolicy

        for lbl in (self.user_id_label, self.user_hash_label, self.balance_label):
            lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # === верхняя панель: слева инфо, справа название+версия сверху ===
        info_box = QWidget()
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(2)
        info_layout.addWidget(self.user_id_label)
        info_layout.addWidget(self.user_hash_label)
        info_layout.addWidget(self.balance_label)

        self.app_title = QLabel(_APP_NAME)
        self.app_title.setStyleSheet("font-weight: 600; font-size: 18px;")
        self.app_title.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.app_version = QLabel(f"v{_APP_VERSION}")
        self.app_version.setStyleSheet("color: #666; font-size: 12px;")
        self.app_version.setAlignment(Qt.AlignmentFlag.AlignRight)

        time_box = QWidget()
        time_layout = QVBoxLayout(time_box)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.addWidget(self.time_gif_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        time_layout.addWidget(self.moscow_time_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        right_box = QWidget()
        right_v = QVBoxLayout(right_box)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(0)
        # ⬇️ теперь без stretch — название и версия будут сверху
        right_v.addWidget(self.app_title, alignment=Qt.AlignmentFlag.AlignRight)
        right_v.addWidget(self.app_version, alignment=Qt.AlignmentFlag.AlignRight)

        top_layout = QGridLayout()
        top_layout.addWidget(info_box, 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        top_layout.addWidget(
            time_box,
            0,
            1,
            alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        )
        top_layout.addWidget(right_box, 0, 2, alignment=Qt.AlignmentFlag.AlignTop)
        top_layout.setColumnStretch(0, 1)
        top_layout.setColumnStretch(1, 1)
        top_layout.setColumnStretch(2, 1)

        self.bot_manager = BotManager()

        self.change_currency_button = QPushButton("Сменить валюту RUB/USD")
        self.change_currency_button.clicked.connect(
            lambda: asyncio.create_task(self.on_change_currency_clicked())
        )

        self.set_risk_button = QPushButton("Настроить риск-менеджмент", self)
        self.set_risk_button.clicked.connect(self._open_risk_dialog)

        self.toggle_demo_button = QPushButton("Переключить Реал/Демо")
        self.toggle_demo_button.clicked.connect(
            lambda: asyncio.create_task(self.on_toggle_demo_clicked())
        )

        self.add_bot_button = QPushButton("Создать бота")
        self.add_bot_button.clicked.connect(self.show_add_bot_dialog)

        self.bot_table = QTableWidget(self)
        self.bot_table.setColumnCount(9)
        self.bot_table.setHorizontalHeaderLabels(
            [
                "Валютная пара",
                "ТФ",
                "Время работы",
                "Статус",
                "Стратегия",
                "Профит",
                "Счёт",
                "Настройки",
                "Управление",
            ]
        )
        hdr = self.bot_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(True)
        self.bot_table.setAlternatingRowColors(True)
        self.bot_table.setSortingEnabled(False)
        self.bot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bot_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.bot_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Маппинги по ботам
        self.bot_rows: dict[Bot, int] = {}
        self.bot_started_at: dict[Bot, float] = {}
        self.bot_profit = defaultdict(float)
        self.bot_status: dict[Bot, str] = {}
        self.bot_runtime_sec: dict[Bot, float] = defaultdict(float)
        self.bot_last_tick: dict[Bot, float] = {}
        self.bot_pause_buttons: dict[Bot, QPushButton] = {}
        self.bot_stop_buttons: dict[Bot, QPushButton] = {}

        # Тикер для апдейта "Время работы"
        self._bots_timer = QTimer(self)
        self._bots_timer.setInterval(1000)
        self._bots_timer.timeout.connect(self._refresh_bot_rows_runtime)
        self._bots_timer.start()

        self.signal_log = QTextEdit()
        self.signal_log.setReadOnly(True)

        # === Таблица результатов сделок ===
        self.trades_table = TradesTableWidget(self)

        layout = QVBoxLayout()
        layout.setMenuBar(self.menu_bar)
        layout.addLayout(top_layout)
        layout.addWidget(self.change_currency_button)
        layout.addWidget(self.set_risk_button)
        layout.addWidget(self.toggle_demo_button)
        layout.addWidget(self.add_bot_button)
        layout.addWidget(QLabel("Список ботов:"))
        layout.addWidget(self.bot_table)
        layout.addWidget(QLabel("Логи:"))
        layout.addWidget(self.signal_log)
        layout.addWidget(QLabel("Сделки:"))
        layout.addWidget(self.trades_table)
        self.setLayout(layout)
        self.resize(1200, 700)

        QTimer.singleShot(0, self.start_async_tasks)

        self._time_timer = QTimer(self)
        self._time_timer.setInterval(1000)
        self._time_timer.timeout.connect(self._update_moscow_time)
        self._update_moscow_time()
        self._time_timer.start()
        self._load_time_gif()

    def strategy_label(self, key: str) -> str:
        return self.strategy_labels.get(key, key)

    # -------------------- async init --------------------
    def start_async_tasks(self):
        from core import ws_client

        ws_client.signal_log_callback = self.append_to_log
        asyncio.create_task(self._init_session_and_loop())
        asyncio.create_task(listen_to_signals())

    async def _init_session_and_loop(self):
        self.http_client = await create_http_client_from_browser_cookies()
        uid, uhash = await extract_user_credentials_from_client(self.http_client)
        if not uid or not uhash:
            self.user_id_label.setText("Ошибка: нет user_id")
            self.user_hash_label.setText("")
            self.balance_label.setText("Баланс: ошибка")
            return

        self.user_id, self.user_hash = uid, uhash
        self.user_id_label.setText(f"user_id: {uid}")
        self.user_hash_label.setText(f"user_hash: {uhash}")

        await self._update_balance_loop()

    async def _update_balance_loop(self):
        while True:
            try:
                demo = await is_demo_account(self.http_client)
                self.is_demo = demo
                amount, currency, _ = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
                self.account_currency = currency
                display = format_money(amount, currency)

                if self.is_demo and "(демо)" not in display:
                    display = f"{display} (демо)"

                self.balance_label.setText(f"Баланс: {display}")
            except Exception as e:
                self.append_to_log(f"[!] Ошибка при получении баланса: {e}")
                self.balance_label.setText("Баланс: ошибка")
            await asyncio.sleep(10)

    # -------------------- logging --------------------

    def _make_bot_logger(self, bot):
        def _log(text: str):
            s = ts(str(text))
            self.bot_logs[bot].append(s)
            for cb in list(self.bot_log_listeners.get(bot, [])):
                try:
                    cb(s)
                except Exception:
                    pass

        return _log

    def append_to_log(self, text: str):
        # self.signal_log.append(str(text))
        s = ts(str(text)) + "\n"
        cur = self.signal_log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Start)  # курсор в начало
        self.signal_log.setTextCursor(cur)
        self.signal_log.insertPlainText(s)  # вставляем новую строку сверху

    def _update_moscow_time(self):
        try:
            moscow_tz = ZoneInfo("Europe/Moscow")
            now_local = datetime.now().astimezone()
            now_moscow = now_local.astimezone(moscow_tz)
            time_text = now_moscow.strftime("%H:%M:%S")
        except Exception:
            time_text = "--:--:--"

        self.moscow_time_label.setText(time_text)

    def _load_time_gif(self):
        gif_path = config.get_time_gif_path()

        if not gif_path:
            self.time_gif_label.setText("Гифка не указана")
            return

        path = Path(gif_path).expanduser()
        if not path.exists():
            self.time_gif_label.setText("Гифка не найдена")
            return

        movie = QMovie(str(path))
        if not movie.isValid():
            self.time_gif_label.setText("Не удалось загрузить гифку")
            return

        movie.setScaledSize(QSize(1200, 120))
        self.time_gif_movie = movie
        self.time_gif_label.setMovie(movie)
        movie.start()

    def _choose_font(self):
        current_font = QApplication.instance().font()
        font, ok = QFontDialog.getFont(current_font, self, "Выбор шрифта")
        if not ok:
            return
        QApplication.instance().setFont(font)
        config.set_font_family(font.family())
        config.set_font_size(font.pointSize())
        config.save_config()

    def _open_templates_dialog(self):
        dlg = TemplatesDialog(self)
        dlg.exec()

    # -------------------- bots --------------------
    def show_add_bot_dialog(self):
        dialog = AddBotDialog(
            available_symbols=self.available_symbols,
            available_strategies=self.available_strategies,
            strategy_labels=self.strategy_labels,
        )
        if not dialog.exec():
            return

        strategy_key = dialog.selected_strategy
        symbol = dialog.selected_symbol
        timeframe = dialog.selected_timeframe

        if not all([self.http_client, self.user_id, self.user_hash]):
            self.append_to_log("❌ Клиент не готов. Подождите...")
            return

        strategy_class = self.available_strategies.get(strategy_key)
        if not strategy_class:
            self.append_to_log(f"❌ Стратегия '{strategy_key}' не найдена.")
            return

        async def _spawn_bot():
            bot_client = await self.http_client.fork()

            # 1) создаём бота БЕЗ колбэков, только с базовыми params
            bot = Bot(
                strategy_cls=strategy_class,
                strategy_kwargs={
                    "http_client": bot_client,
                    "user_id": self.user_id,
                    "user_hash": self.user_hash,
                    "symbol": symbol,
                    "strategy_key": strategy_key,
                    "log_callback": None,  # выставим ниже через _make_bot_logger
                    "timeframe": timeframe,
                    "params": {
                        "account_currency": getattr(self, "account_currency", "RUB"),
                        # если выбран режим "все валютные пары" — передаём полный список
                        **(
                            {
                                "symbols": self.available_symbols
                            }
                            if symbol == ALL_SYMBOLS_LABEL
                            else {}
                        ),
                    },
                },
                on_log=self.append_to_log,
                on_finish=lambda b=None: self.on_bot_finished(bot),
            )

            # 2) теперь безопасно навесим колбэки, ссылающиеся на bot
            params = bot.strategy_kwargs.setdefault("params", {})
            params.update(
                {
                    "on_trade_result": lambda **kw: self._on_bot_trade_result(
                        bot, **kw
                    ),
                    "on_trade_pending": lambda **kw: self._on_bot_trade_pending(
                        bot, **kw
                    ),  # 👈 теперь знаем bot
                    "on_status": lambda s, b=bot: self._set_bot_status(b, s),
                }
            )

            # 3) логгер уже можно привязать к конкретному боту
            bot.strategy_kwargs["log_callback"] = self._make_bot_logger(bot)

            # 4) регистрируем бота в менеджере
            self.bot_manager.add_bot(bot)

            # 5) добавляем строку в ТАБЛИЦУ ботов
            row = self.bot_table.rowCount()
            self.bot_table.insertRow(row)
            self.bot_rows[bot] = row
            self.bot_started_at[bot] = asyncio.get_running_loop().time()
            self.bot_status[bot] = "выключен"  # до запуска

            strategy_label = self.strategy_label(strategy_key)
            account_txt = "ДЕМО" if self.is_demo else "РЕАЛ"

            def _set(r, c, text):
                self.bot_table.setItem(r, c, QTableWidgetItem(str(text)))

            _set(row, 0, symbol)  # Пара
            _set(row, 1, timeframe)  # ТФ
            _set(row, 2, "0:00")  # Время работы
            _set(row, 3, self.bot_status[bot])  # Статус
            _set(row, 4, strategy_label)  # Стратегия
            profit_item = QTableWidgetItem(format_money(0, self.account_currency))
            self.bot_table.setItem(row, 5, profit_item)
            _set(row, 6, account_txt)  # Счёт

            btn_open = QPushButton("Открыть", self)
            btn_open.clicked.connect(partial(self.open_strategy_control_dialog, bot))
            self.bot_table.setCellWidget(row, 7, btn_open)

            ctrl_widget = QWidget()
            hl = QHBoxLayout(ctrl_widget)
            hl.setContentsMargins(0, 0, 0, 0)
            btn_pause = QPushButton("⏸", self)
            btn_pause.setEnabled(False)
            btn_pause.clicked.connect(partial(self._toggle_pause_clicked, bot))
            btn_stop = QPushButton("⏹", self)
            btn_stop.setEnabled(False)
            btn_stop.clicked.connect(partial(self.stop_bot, bot))
            btn_del = QPushButton("❌", self)
            btn_del.clicked.connect(partial(self.delete_bot, bot))
            hl.addWidget(btn_pause)
            hl.addWidget(btn_stop)
            hl.addWidget(btn_del)
            self.bot_table.setCellWidget(row, 8, ctrl_widget)
            self.bot_pause_buttons[bot] = btn_pause
            self.bot_stop_buttons[bot] = btn_stop

            self.bot_runtime_sec[bot] = 0.0
            self.bot_last_tick[bot] = asyncio.get_running_loop().time()

            self.append_to_log(
                f"🤖 Создан бот: {strategy_label} [{symbol} {timeframe}]. Откройте настройки, чтобы запустить."
            )

            # Автоматически открываем окно управления, чтобы задать параметры и запустить
            try:
                self.open_strategy_control_dialog(bot)
            except Exception:
                pass

        asyncio.create_task(_spawn_bot())

    def open_strategy_control_dialog(self, bot):
        from gui.strategy_control_dialog import StrategyControlDialog

        existing = self.strategy_windows.get(bot)
        if existing is not None:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return

        dlg = StrategyControlDialog(self, bot)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.strategy_windows[bot] = dlg

        def _cleanup(*_):
            self.strategy_windows.pop(bot, None)

        dlg.destroyed.connect(_cleanup)
        dlg.show()

    def stop_bot(self, bot):
        bot.stop()
        self.on_bot_finished(bot)
        btn = self.bot_stop_buttons.get(bot)
        if btn:
            btn.setEnabled(False)

    def toggle_pause(self, bot, paused: bool):
        has_started = getattr(bot, "has_started", None)
        started = (
            bot.has_started()
            if callable(has_started)
            else getattr(bot, "_strategy", None) is not None
        )

        key = bot.strategy_kwargs.get("strategy_key", "")
        label = self.strategy_label(key)
        sym = bot.strategy_kwargs.get("symbol")

        if not started:
            bot.start()
            self.append_to_log(f"▶ Запуск: {label} [{sym}]")
            if paused:
                bot.pause()
                self.append_to_log("⏸ Пауза.")
            else:
                bot.resume()
                self.append_to_log("▶ Работает.")
            return

        if paused:
            bot.pause()
            self.append_to_log(f"⏸ Пауза: {label} [{sym}]")
        else:
            bot.resume()
            self.append_to_log(f"▶ Продолжить: {label} [{sym}]")

    def _toggle_pause_clicked(self, bot):
        st = bot.strategy
        paused = bool(st and hasattr(st, "is_paused") and st.is_paused())
        self.toggle_pause(bot, not paused)
        btn = self.bot_pause_buttons.get(bot)
        if btn:
            btn.setText("⏸" if paused else "▶")

    def delete_bot(self, bot):
        row = self.bot_rows.pop(bot, None)
        self.bot_manager.remove_bot(bot)
        if row is not None and 0 <= row < self.bot_table.rowCount():
            self.bot_table.removeRow(row)
            for b, r in list(self.bot_rows.items()):
                if r > row:
                    self.bot_rows[b] = r - 1

        pending_ids = self.bot_pending_trades.pop(bot, set())
        for tid in pending_ids:
            try:
                self.trades_table.remove_trade(str(tid))
            except Exception:
                pass
        for mp in (
            self.bot_started_at,
            self.bot_profit,
            self.bot_status,
            self.bot_runtime_sec,
            self.bot_last_tick,
            getattr(self, "bot_last_phase", {}),
            self.bot_logs,
            self.bot_log_listeners,
            self.bot_trade_listeners,
            self.bot_trade_history,
        ):
            mp.pop(bot, None)
        self.bot_pause_buttons.pop(bot, None)
        self.bot_stop_buttons.pop(bot, None)
        key = bot.strategy_kwargs.get("strategy_key", "")
        sym = bot.strategy_kwargs.get("symbol")
        label = self.strategy_label(key)
        self.append_to_log(f"× Удалён бот: {label} [{sym}]")

    def open_settings_dialog(self, bot):
        from gui.settings_factory import get_settings_dialog_cls

        dlg_cls = get_settings_dialog_cls(bot.strategy_cls)
        if not dlg_cls:
            self.append_to_log("⚠ Нет окна настроек для этой стратегии.")
            return

        live_params = {}
        if getattr(bot, "strategy", None):
            if bot.strategy:
                live_params = dict(getattr(bot.strategy, "params", {}) or {})
        if not live_params:
            base_params = {}
            if isinstance(bot.strategy_kwargs, dict):
                base_params = dict(bot.strategy_kwargs.get("params", {}))
            live_params = base_params
        live_params.setdefault("timeframe", bot.strategy_kwargs.get("timeframe", "M1"))
        live_params.setdefault("symbol", bot.strategy_kwargs.get("symbol", ""))

        dlg = dlg_cls(live_params, parent=self)
        if not dlg.exec():
            return

        new_params = dlg.get_params()
        if isinstance(bot.strategy_kwargs, dict):
            bot.strategy_kwargs.setdefault("params", {}).update(new_params)
        if getattr(bot, "strategy", None) and bot.strategy:
            try:
                bot.strategy.update_params(**new_params)
            except AttributeError:
                self.append_to_log(
                    "⚠ Стратегия не поддерживает update_params(). Обновите базовый класс."
                )
                return
        self.append_to_log(f"⚙ Сохранены настройки: {new_params}")

    def _on_item_double_clicked(self, item):
        for bot, it in self.bot_items.items():
            if it is item:
                self.open_strategy_control_dialog(bot)
                break

    async def on_change_currency_clicked(self):
        try:
            ok = await change_currency(self.http_client, self.user_id, self.user_hash)
            if ok:
                await refresh_http_client_cookies(self.http_client)
                uid, uhash = await extract_user_credentials_from_client(
                    self.http_client
                )
                if uid and uhash:
                    self.user_id, self.user_hash = uid, uhash
                self.append_to_log("✅ Валюта/режим обновлены, куки перезагружены.")
            else:
                self.append_to_log("❌ Не удалось сменить валюту/режим.")
        except Exception as e:
            self.append_to_log(f"❌ Ошибка смены валюты/режима: {e}")

    async def on_toggle_demo_clicked(self):
        """
        Переключить режим Реал/Демо через user_real_trade.php.
        После переключения — обновляем куки и user_id/user_hash,
        и сразу же перечитываем статус демо + баланс.
        """
        if not all([self.http_client, self.user_id, self.user_hash]):
            self.append_to_log("❌ Клиент не готов. Подождите...")
            return
        try:
            ok = await toggle_real_demo(self.http_client, self.user_id, self.user_hash)
            if not ok:
                self.append_to_log("❌ Не удалось переключить режим Реал/Демо.")
                return

            # Обновим куки/учётки — как после смены валюты
            await refresh_http_client_cookies(self.http_client)
            uid, uhash = await extract_user_credentials_from_client(self.http_client)
            if uid and uhash:
                self.user_id, self.user_hash = uid, uhash

            # Сразу перечитаем статус и баланс, чтобы UI обновился мгновенно
            self.is_demo = await is_demo_account(self.http_client)
            amount, currency, _ = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            self.account_currency = currency
            display = format_money(amount, currency)
            if self.is_demo and "(демо)" not in display:
                display = f"{display} (демо)"
            self.balance_label.setText(f"Баланс: {display}")

            mode = "ДЕМО" if self.is_demo else "РЕАЛ"
            self.append_to_log(f"✅ Переключено. Текущий режим: {mode}.")
        except Exception as e:
            self.append_to_log(f"❌ Ошибка переключения Реал/Демо: {e}")

    def _open_risk_dialog(self):
        res = RiskDialog.get_values(self, min_default=75, max_default=200)
        if res is None:
            return
        min_v, max_v = res
        if max_v < min_v:
            # не модально-блокирующий способ
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Проверка")
            box.setText("Максимум не может быть меньше минимума.")
            box.open()
            return
        # ВАЖНО: запускаем корутину как задачу, а не через asyncSlot
        asyncio.create_task(self._apply_risk_limits(min_v, max_v))

    async def _apply_risk_limits(self, min_v: int, max_v: int):
        # (по желанию) временно выключим кнопку, чтобы избежать дабл-кликов
        if hasattr(self, "btnRisk"):
            self.btnRisk.setEnabled(False)
        try:
            ok = await set_risk(
                self.http_client,
                self.user_id,
                self.user_hash,
                risk_min=min_v,
                risk_max=max_v,
            )
            # НЕ вызываем статические QMessageBox.information/critical (они модальные)
            box = QMessageBox(self)
            box.setIcon(
                QMessageBox.Icon.Information if ok else QMessageBox.Icon.Critical
            )
            box.setWindowTitle("Готово" if ok else "Ошибка")
            box.setText(
                f"Ежедневное ограничение установлено:\nМинимум: {min_v}%\nМаксимум: {max_v}%"
                if ok
                else "Не удалось установить лимит риска (сервер вернул ошибку)."
            )
            box.open()  # не блокирует цикл
        except Exception as e:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Ошибка")
            box.setText(f"Не удалось установить лимит риска:\n{e}")
            box.open()
        finally:
            if hasattr(self, "btnRisk"):
                self.btnRisk.setEnabled(True)

    def add_trade_pending(
        self,
        *,
        trade_id: str,
        signal_at: str,
        placed_at: str,
        strategy: str | None = None,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        wait_seconds: float,
        account_mode: str | None = None,
        indicator: str | None = None,
        series: str | None = None,
        expected_end_ts: float | None = None,
        step: str | None = None,
    ):
        """Добавляет строку ожидания сделки."""
        duration = float(wait_seconds)
        acc = account_mode or ("ДЕМО" if self.is_demo else "РЕАЛ")
        self.trades_table.add_pending(
            trade_id=str(trade_id),
            signal_at=signal_at,
            placed_at=placed_at,
            strategy=strategy or "-",
            indicator=indicator or "-",
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            stake=float(stake),
            duration=duration,
            percent=int(percent),
            account_mode=acc,
            series=series,
            expected_end_ts=expected_end_ts,
            currency=self.account_currency,
            step=step,
        )

    def add_trade_result(
        self,
        *,
        trade_id: str | None = None,
        signal_at: str,
        placed_at: str,
        strategy: str | None = None,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        profit: float | None,
        account_mode: str | None = None,
        indicator: str | None = None,  # <= НОВОЕ
        series: str | None = None,
        step: str | None = None,
        ):
        """Добавляет результат сделки в таблицу."""
        acc = account_mode or ("ДЕМО" if self.is_demo else "РЕАЛ")
        tid = str(trade_id) if trade_id is not None else ""

        if tid not in getattr(self.trades_table, "_row_by_trade", {}):
            # если не было pending, добавим строку с базовой информацией
            self.trades_table.add_pending(
                trade_id=tid or "-",
                signal_at=signal_at,
                placed_at=placed_at,
                strategy=strategy or "-",
                indicator=indicator or "-",
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                stake=float(stake),
                duration=0.0,
                percent=int(percent),
                account_mode=acc,
                series=series,
                step=step,
                currency=self.account_currency,
            )

        self.trades_table.set_result(tid or "-", profit, self.account_currency)

    def _fmt_runtime(self, seconds: float) -> str:
        s = int(max(0, round(seconds)))
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _fmt_profit(self, value: float) -> str:
        try:
            v = float(value)
        except Exception:
            v = 0.0
        ccy = getattr(self, "account_currency", "RUB")
        return format_money(v, ccy, show_plus=True)

    def _refresh_bot_rows_runtime(self):
        now = asyncio.get_running_loop().time()

        for bot, row in list(self.bot_rows.items()):
            # строка могла уже быть удалена
            if row is None or row >= self.bot_table.rowCount():
                continue

            # состояние бота/стратегии
            has_started_fn = getattr(bot, "has_started", None)
            started = bool(has_started_fn() if callable(has_started_fn) else False)
            running = bool(getattr(bot, "is_running", lambda: False)())
            st = bot.strategy
            paused = bool(st and hasattr(st, "is_paused") and st.is_paused())

            # === Время работы ===
            last = self.bot_last_tick.get(bot, now)
            # накапливаем только когда реально работает и не на паузе
            if started and running and not paused:
                self.bot_runtime_sec[bot] = self.bot_runtime_sec.get(bot, 0.0) + (
                    now - last
                )
            # обновляем last_tick всегда, чтобы время не "капало" на паузе
            self.bot_last_tick[bot] = now

            # отрисовать время (колонка 2)
            secs = self.bot_runtime_sec.get(bot, 0.0)
            it_time = self.bot_table.item(row, 2)
            if it_time is None:
                it_time = QTableWidgetItem()
                self.bot_table.setItem(row, 2, it_time)
            it_time.setText(self._fmt_runtime(secs))

            # === Статус ===
            # Если пауза — показываем "пауза", иначе последнюю фазу от стратегии (или кэш)
            last_phase = getattr(self, "bot_last_phase", {}).get(
                bot, None
            ) or self.bot_status.get(bot, "—")
            ui_status = "пауза" if paused else last_phase

            it_status = self.bot_table.item(row, 3)  # колонка «Статус»
            if it_status is None:
                it_status = QTableWidgetItem()
                self.bot_table.setItem(row, 3, it_status)
            it_status.setText(ui_status)

            btn = self.bot_pause_buttons.get(bot)
            if btn:
                btn.setEnabled(started)
                btn.setText("▶" if paused else "⏸")

            btn_stop = self.bot_stop_buttons.get(bot)
            if btn_stop:
                btn_stop.setEnabled(started)

    def _set_bot_status(self, bot, status: str):
        """Колбэк от стратегии: 'ожидание сигнала' / 'делает ставку' / 'ожидание результата'.
        Статус 'пауза' НЕ принимаем отсюда — его рисует UI по is_paused() (вариант Б).
        """
        # Кэшируем последнюю НЕ-паузную фазу
        s = (status or "—").strip()
        self.bot_last_phase[bot] = s

        row = self.bot_rows.get(bot)
        if row is None or row >= self.bot_table.rowCount():
            return

        # Если бот на паузе — показываем 'пауза', иначе последнюю фазу
        st = bot.strategy
        ui_status = (
            "пауза" if (st and hasattr(st, "is_paused") and st.is_paused()) else s
        )

        it = self.bot_table.item(row, 3)  # колонка «Статус»
        if it is None:
            it = QTableWidgetItem()
            self.bot_table.setItem(row, 3, it)
        it.setText(ui_status)

    def _on_bot_trade_result(self, bot, **kw):
        try:
            profit = kw.get("profit", None)
            if profit is not None:
                self.bot_profit[bot] += float(profit)

            # обновим таблицу
            row = self.bot_rows.get(bot)
            if row is not None and row < self.bot_table.rowCount():
                item = self.bot_table.item(row, 5)  # колонка "Профит"
                if item is None:
                    item = QTableWidgetItem()
                    self.bot_table.setItem(row, 5, item)

                total = self.bot_profit[bot]
                cur = getattr(self, "account_currency", "RUB")
                text = format_money(total, cur)
                # если положительный — добавляем "+"
                if total > 0:
                    text = "+" + text
                    item.setForeground(QColor("green"))
                elif total < 0:
                    item.setForeground(QColor("red"))
                else:
                    item.setForeground(QColor("black"))
                item.setText(text)
        except Exception as e:
            self.append_to_log(f"[!] Ошибка обновления профита: {e}")

        tid = str(kw.get("trade_id", ""))
        if tid:
            self.bot_pending_trades.get(bot, set()).discard(tid)

        indicator = kw.get("indicator")
        if not indicator:
            indicator = (kw.get("meta") or {}).get("indicator")
            if indicator:
                kw = dict(kw)
                kw["indicator"] = indicator

        # дальше — обычное добавление в таблицу сделок
        key = bot.strategy_kwargs.get("strategy_key", "")
        strat_label = self.strategy_label(key)
        self.add_trade_result(**kw, strategy=strat_label)
        # кэшируем для истории
        self.bot_trade_history[bot].append(("result", dict(kw)))
        # 👇 уведомим всех подписчиков для этого бота (открытые StrategyControlDialog)
        for cb in list(self.bot_trade_listeners.get(bot, [])):
            try:
                cb("result", kw)
            except Exception:
                pass

    def _on_bot_trade_pending(self, bot, **kw):
        """
        Сначала — в общую таблицу, затем — уведомляем окна стратегии ЭТОГО бота.
        Прокидываем expected_end_ts, чтобы их таймеры были синхронными.
        """
        from time import time as _now

        payload = dict(kw)
        if not payload.get("indicator"):
            indicator = (payload.get("meta") or {}).get("indicator")
            if indicator:
                payload["indicator"] = indicator
        wait_seconds = float(payload.get("wait_seconds", 0.0))
        expected_end_ts = payload.get("expected_end_ts")
        if expected_end_ts is None:
            expected_end_ts = _now() + wait_seconds
        payload["expected_end_ts"] = float(expected_end_ts)

        # В общую (главную) таблицу
        key = bot.strategy_kwargs.get("strategy_key", "")
        payload["strategy"] = self.strategy_label(key)
        try:
            self.add_trade_pending(**payload)
        except Exception:
            pass

        tid = str(payload.get("trade_id", ""))
        if tid:
            self.bot_pending_trades[bot].add(tid)

        # ⬇️ НОВОЕ: сохраняем в историю, чтобы StrategyControlDialog восстановил «ожидание»
        self.bot_trade_history[bot].append(("pending", dict(payload)))

        # Уведомляем открытые окна конкретного бота
        for cb in list(self.bot_trade_listeners.get(bot, [])):
            try:
                cb("pending", payload)
            except Exception:
                pass

    def on_bot_finished(self, bot):
        # Просто помечаем статус, оставляя строку в таблице — бот можно перезапустить
        row = self.bot_rows.get(bot)
        self.bot_status[bot] = "стратегия завершена"
        self.bot_last_phase[bot] = "стратегия завершена"

        if row is not None and 0 <= row < self.bot_table.rowCount():
            it = self.bot_table.item(row, 3)
            if it is None:
                it = QTableWidgetItem()
                self.bot_table.setItem(row, 3, it)
            it.setText("стратегия завершена")

        key = bot.strategy_kwargs.get("strategy_key", "")
        label = self.strategy_label(key)
        self.append_to_log(
            f"ℹ️ Бот завершил работу: {label} [{bot.strategy_kwargs.get('symbol')}]"
        )

    def reset_bot(self, bot):
        """Очистить состояние бота перед повторным запуском."""
        # сбросим накопленные значения
        self.bot_runtime_sec[bot] = 0.0
        self.bot_profit[bot] = 0.0
        self.bot_status[bot] = "выключен"
        self.bot_last_phase[bot] = "выключен"
        loop = asyncio.get_running_loop()
        self.bot_started_at[bot] = loop.time()
        self.bot_last_tick[bot] = loop.time()

        row = self.bot_rows.get(bot)
        if row is not None and row < self.bot_table.rowCount():
            # время работы
            it_time = self.bot_table.item(row, 2)
            if it_time is None:
                it_time = QTableWidgetItem()
                self.bot_table.setItem(row, 2, it_time)
            it_time.setText("0:00")

            # статус
            it_status = self.bot_table.item(row, 3)
            if it_status is None:
                it_status = QTableWidgetItem()
                self.bot_table.setItem(row, 3, it_status)
            it_status.setText("выключен")

            # профит
            it_profit = self.bot_table.item(row, 5)
            if it_profit is None:
                it_profit = QTableWidgetItem()
                self.bot_table.setItem(row, 5, it_profit)
            it_profit.setForeground(QColor("black"))
            it_profit.setText(format_money(0, self.account_currency))

    def _on_trade_pending_global(
        self,
        *,
        trade_id,
        signal_at,
        symbol,
        timeframe,
        placed_at,
        direction,
        stake,
        percent,
        wait_seconds,
        account_mode,
        indicator: str = "-",
        series: str | None = None,
        step: str | None = None,
        bot=None,
    ):
        strat_label = "-"
        if bot is not None:
            key = bot.strategy_kwargs.get("strategy_key", "")
            strat_label = self.strategy_label(key)
        self.add_trade_pending(
            trade_id=trade_id,
            signal_at=signal_at,
            placed_at=placed_at,
            strategy=strat_label,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            stake=stake,
            percent=percent,
            wait_seconds=wait_seconds,
            account_mode=account_mode,
            indicator=indicator,
            series=series,
            step=step,
        )
        # (при желании: дублируем в локальные таблицы окон стратегий)

    def _on_trade_result_global(
        self,
        *,
        trade_id,
        signal_at,
        symbol,
        timeframe,
        placed_at,
        direction,
        stake,
        percent,
        profit,
        account_mode,
        indicator: str = "-",
        series: str | None = None,
        step: str | None = None,
        bot=None,
    ):
        strat_label = "-"
        if bot is not None:
            key = bot.strategy_kwargs.get("strategy_key", "")
            strat_label = self.strategy_label(key)
        self.add_trade_result(
            trade_id=trade_id,
            signal_at=signal_at,
            placed_at=placed_at,
            strategy=strat_label,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            stake=stake,
            percent=percent,
            profit=profit,
            account_mode=account_mode,
            indicator=indicator,
            series=series,
            step=step,
        )
