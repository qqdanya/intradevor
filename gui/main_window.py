# gui/main_window.py
from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QTextEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
)
from PyQt6.QtGui import QTextCursor
from PyQt6.QtCore import QTimer
from collections import defaultdict
from functools import partial
import asyncio

from gui.bot_add_dialog import AddBotDialog
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
)
from core.ws_client import listen_to_signals
from core.bot_manager import BotManager
from core.bot import Bot
from strategies.martingale import MartingaleStrategy


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.http_client = None
        self.user_id = None
        self.user_hash = None
        self.account_currency = "RUB"
        self.is_demo = False

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

        self.change_currency_button = QPushButton("Сменить валюту")
        self.change_currency_button.clicked.connect(
            lambda: asyncio.create_task(self.on_change_currency_clicked())
        )

        self.toggle_demo_button = QPushButton("Переключить Реал/Демо")
        self.toggle_demo_button.clicked.connect(
            lambda: asyncio.create_task(self.on_toggle_demo_clicked())
        )

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
        layout.addWidget(self.toggle_demo_button)
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
                amount, currency, display = await get_balance_info(
                    self.http_client, self.user_id, self.user_hash
                )
                self.account_currency = currency

                if self.is_demo and "(демо)" not in display:
                    display = f"{display} (демо)"

                self.balance_label.setText(f"Баланс: {display}")
            except Exception as e:
                print(f"[!] Ошибка при получении баланса: {e}")
                self.balance_label.setText("Баланс: ошибка")
            await asyncio.sleep(5)

    # -------------------- logging --------------------

    def _make_bot_logger(self, bot):
        def _log(text: str):
            s = str(text)
            self.bot_logs[bot].append(s)
            for cb in list(self.bot_log_listeners.get(bot, [])):
                try:
                    cb(s)
                except Exception:
                    pass

        return _log

    def append_to_log(self, text: str):
        # self.signal_log.append(str(text))
        s = str(text) + "\n"
        cur = self.signal_log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Start)  # курсор в начало
        self.signal_log.setTextCursor(cur)
        self.signal_log.insertPlainText(s)  # вставляем новую строку сверху

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

        # ⬇️ ВАЖНО: форкаем клиента — бот работает на «замороженных» куках
        async def _spawn_bot():
            bot_client = await self.http_client.fork()

            bot = Bot(
                strategy_cls=strategy_class,
                strategy_kwargs={
                    "http_client": bot_client,
                    "user_id": self.user_id,
                    "user_hash": self.user_hash,
                    "symbol": symbol,
                    "strategy_key": strategy_key,
                    "log_callback": None,  # временно
                    "timeframe": timeframe,
                    "params": {
                        "account_currency": getattr(self, "account_currency", "RUB")
                    },
                },
                on_log=self.append_to_log,
                on_finish=lambda b=None: self.on_bot_finished(bot),
            )
            bot.strategy_kwargs["log_callback"] = self._make_bot_logger(bot)

            self.bot_manager.add_bot(bot)

            from gui.bot_item_widget import BotItemWidget

            item = QListWidgetItem()
            title = f"{self.strategy_label(strategy_key)} [{symbol} {timeframe}]"
            widget = BotItemWidget(
                title=title,
                on_settings=partial(self.open_strategy_control_dialog, bot),
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

        asyncio.create_task(_spawn_bot())

    def open_strategy_control_dialog(self, bot):
        from gui.strategy_control_dialog import StrategyControlDialog

        dlg = StrategyControlDialog(self, bot, parent=self)
        dlg.exec()

    def on_bot_finished(self, bot):
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

        live_params = {}
        if getattr(bot, "strategy", None):
            if bot.strategy:
                live_params = dict(getattr(bot.strategy, "params", {}) or {})
        if not live_params:
            base_params = {}
            if isinstance(bot.strategy_kwargs, dict):
                base_params = dict(bot.strategy_kwargs.get("params", {}))
            live_params = base_params

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
            amount, currency, display = await get_balance_info(
                self.http_client, self.user_id, self.user_hash
            )
            self.account_currency = currency
            if self.is_demo and "(демо)" not in display:
                display = f"{display} (демо)"
            self.balance_label.setText(f"Баланс: {display}")

            mode = "ДЕМО" if self.is_demo else "РЕАЛ"
            self.append_to_log(f"✅ Переключено. Текущий режим: {mode}.")
        except Exception as e:
            self.append_to_log(f"❌ Ошибка переключения Реал/Демо: {e}")
