from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QTextEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
)
from PyQt6.QtCore import QTimer
from gui.bot_add_dialog import AddBotDialog
from core.session import (
    create_session_from_browser_cookies,
    extract_user_credentials,
    save_session,
    load_session,
)
from core.intrade_api import get_balance_info, change_currency
from core.ws_client import listen_to_signals
from core.bot_manager import BotManager
from core.bot import Bot
from strategies.martingale import MartingaleStrategy
from collections import defaultdict
from gui.strategy_control_dialog import StrategyControlDialog
from functools import partial
import asyncio


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.session = None
        self.user_id = None
        self.user_hash = None

        self.available_symbols = [
            "AUDCAD",
            "AUDCHF",
            "AUDJPY",
            "AUDNZD",
            "AUDUSD",
            "CADJPY",
            "EURAUD",
            "EURCAD",
            "EURCHF",
            "EURGBP",
            "EURJPY",
            "EURUSD",
            "GBPAUD",
            "GBPCHF",
            "GBPJPY",
            "GBPNZD",
            "NZDJPY",
            "NZDUSD",
            "USDCAD",
            "USDCHF",
            "USDJPY",
            "BTCUSDT",
        ]
        self.available_strategies = {
            "martingale": MartingaleStrategy,
            # "oscar_grind": OscarGrindStrategy,
        }

        self.strategy_labels = {
            "martingale": "Мартингейл",
        }

        self.bot_items = {}
        self.bot_widgets = {}
        self.bot_logs = defaultdict(list)
        self.bot_log_listeners = defaultdict(list)

        self.user_id_label = QLabel("user_id: loading...")
        self.user_hash_label = QLabel("user_hash: loading...")
        self.balance_label = QLabel("Баланс: loading...")

        self.bot_manager = BotManager()

        self.change_currency_button = QPushButton("Сменить")
        self.change_currency_button.clicked.connect(self.on_change_currency_clicked)

        self.add_bot_button = QPushButton("Новый бот")
        self.add_bot_button.clicked.connect(self.show_add_bot_dialog)

        self.bot_list = QListWidget()
        self.bot_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.bot_list.itemDoubleClicked.connect(self._on_item_double_clicked)

        self.signal_log = QTextEdit()
        self.signal_log.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addWidget(self.user_id_label)
        layout.addWidget(self.user_hash_label)
        layout.addWidget(self.balance_label)
        layout.addWidget(self.change_currency_button)
        layout.addWidget(self.add_bot_button)
        layout.addWidget(QLabel("Список ботов:"))
        layout.addWidget(self.bot_list)
        layout.addWidget(self.signal_log)
        self.setLayout(layout)

        QTimer.singleShot(0, self.start_async_tasks)

    def strategy_label(self, key: str) -> str:
        return self.strategy_labels.get(key, key)

    # -------------------- async init --------------------
    def start_async_tasks(self):
        from core import ws_client

        ws_client.signal_log_callback = self.append_to_log
        asyncio.create_task(self._init_session_and_loop())
        asyncio.create_task(listen_to_signals())

    async def _init_session_and_loop(self):
        session = await asyncio.to_thread(load_session)
        if not session:
            session = await asyncio.to_thread(create_session_from_browser_cookies)
            if session:
                await asyncio.to_thread(save_session, session)

        user_id, user_hash = await asyncio.to_thread(extract_user_credentials, session)
        if not user_id or not user_hash:
            self.user_id_label.setText("Ошибка: нет user_id")
            self.user_hash_label.setText("")
            self.balance_label.setText("Баланс: ошибка")
            return

        self.session = session
        self.user_id = user_id
        self.user_hash = user_hash

        self.user_id_label.setText(f"user_id: {user_id}")
        self.user_hash_label.setText(f"user_hash: {user_hash}")

        await self._update_balance_loop()

    async def _update_balance_loop(self):
        while True:
            try:
                # сетевой запрос в отдельном потоке, чтобы не блокировать event loop
                amount, currency, display = await asyncio.to_thread(
                    get_balance_info, self.session, self.user_id, self.user_hash
                )
                # в label выводим уже готовую строку с символом валюты
                self.balance_label.setText(f"Баланс: {display}")

                # сохраняем валюту ТОЛЬКО для будущих ботов (не пушим в уже запущенные)
                self.account_currency = currency

            except Exception as e:
                print(f"[!] Ошибка при получении баланса: {e}")
                self.balance_label.setText("Баланс: ошибка")

            await asyncio.sleep(5)

    # -------------------- logging --------------------

    def _make_bot_logger(self, bot):
        def _log(text: str):
            s = str(text)
            # общий лог
            # self.append_to_log(s)
            # буфер бота
            self.bot_logs[bot].append(s)
            # живые слушатели (диалоги)
            for cb in list(self.bot_log_listeners.get(bot, [])):
                try:
                    cb(s)
                except Exception:
                    pass

        return _log

    def append_to_log(self, text: str):
        self.signal_log.append(str(text))

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

        if not all([self.session, self.user_id, self.user_hash]):
            self.append_to_log("❌ Сессия не готова. Подождите...")
            return

        strategy_class = self.available_strategies.get(strategy_key)

        if not strategy_class:
            self.append_to_log(f"❌ Стратегия '{strategy_key}' не найдена.")
            return

        # ВАЖНО: создаём Bot БЕЗ автозапуска
        bot = Bot(
            strategy_cls=strategy_class,
            strategy_kwargs={
                "session": self.session,
                "user_id": self.user_id,
                "user_hash": self.user_hash,
                "symbol": symbol,
                "strategy_key": strategy_key,
                "log_callback": None,  # временно
                "timeframe": timeframe,
            },
            on_log=self.append_to_log,
            on_finish=lambda b=None: self.on_bot_finished(bot),
        )
        # после создания — назначаем пер-ботовый логгер
        bot.strategy_kwargs["log_callback"] = self._make_bot_logger(bot)

        self.bot_manager.add_bot(bot)

        # элемент UI
        from gui.bot_item_widget import BotItemWidget

        item = QListWidgetItem()
        title = f"{self.strategy_label(strategy_key)} [{symbol} {timeframe}]"
        from functools import partial

        widget = BotItemWidget(
            title=title,
            on_settings=partial(self.open_strategy_control_dialog, bot),  # ← вот так
            on_pause_resume=lambda paused, b=bot: self.toggle_pause(b, paused),
            on_stop=partial(self.stop_bot, bot),
        )

        item.setSizeHint(widget.sizeHint())
        self.bot_list.addItem(item)
        self.bot_list.setItemWidget(item, widget)

        self.bot_items[bot] = item
        self.bot_widgets[bot] = widget

        self.append_to_log(
            f"🤖 Создан бот: {self.strategy_label(strategy_key)} [{symbol}]. Нажмите ▶, чтобы запустить."
        )

    def open_strategy_control_dialog(self, bot):
        dlg = StrategyControlDialog(self, bot, parent=self)
        dlg.exec()

    def on_bot_finished(self, bot):
        # удаляем элемент из списка UI и менеджера
        item = self.bot_items.pop(bot, None)
        self.bot_widgets.pop(bot, None)
        if item is not None:
            row = self.bot_list.row(item)
            self.bot_list.takeItem(row)
        try:
            self.bot_manager.remove_bot(bot)
        except Exception:
            pass

        key = bot.strategy_kwargs.get("strategy_key", "")
        label = self.strategy_label(key)

        self.append_to_log(
            f"ℹ️ Бот завершил работу: {label} [{bot.strategy_kwargs.get('symbol')}]"
        )

    def stop_bot(self, bot):
        bot.stop()
        # по текущей логике on_finish вызовется из Bot._run(),
        # но если стратегия остановилась очень быстро, подстрахуемся:
        # (оставим on_bot_finished вызываться из on_finish)

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

    def open_settings_dialog(self, bot):
        from gui.settings_factory import get_settings_dialog_cls

        dlg_cls = get_settings_dialog_cls(bot.strategy_cls)
        if not dlg_cls:
            self.append_to_log("⚠ Нет окна настроек для этой стратегии.")
            return

        # Текущие параметры: если стратегия запущена — берём живые; иначе — то, что в kwargs
        live_params = {}
        if getattr(bot, "strategy", None):
            if bot.strategy:
                live_params = dict(getattr(bot.strategy, "params", {}) or {})
        if not live_params:
            # читаем дефолт для рестартов
            base_params = {}
            if isinstance(bot.strategy_kwargs, dict):
                base_params = dict(bot.strategy_kwargs.get("params", {}))
            live_params = base_params

        dlg = dlg_cls(live_params, parent=self)
        if not dlg.exec():
            return

        new_params = dlg.get_params()
        # 1) сохраняем как дефолты для будущих запусков/рестартов
        if isinstance(bot.strategy_kwargs, dict):
            bot.strategy_kwargs.setdefault("params", {}).update(new_params)
        # 2) если стратегия уже запущена — обновляем на лету
        if getattr(bot, "strategy", None) and bot.strategy:
            # Требует, чтобы StrategyBase имела update_params(**params)
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

    def on_change_currency_clicked(self):
        change_currency(self.session, self.user_id, self.user_hash)
