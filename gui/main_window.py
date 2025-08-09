from PyQt6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QListWidget, QListWidgetItem, QDialog, QFormLayout, QLineEdit, QDialogButtonBox
)
from PyQt6.QtCore import QTimer
from gui.bot_add_dialog import AddBotDialog
from core.session import create_session_from_browser_cookies, extract_user_credentials
from core.intrade_api import get_balance
from core.ws_client import listen_to_signals, signal_log_callback
from core.bot_manager import BotManager
from core.bot import Bot
from strategies.martingale import MartingaleStrategy
from functools import partial
import asyncio




class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.session = None
        self.user_id = None
        self.user_hash = None
        
        self.available_symbols = ["EURUSD", "GBPUSD", "AUDUSD"]
        self.available_strategies = {
            "martingale": MartingaleStrategy,
            #"Оскар Грайнд": OscarGrindStrategy
        }

        self.bot_items = {}
        self.bot_widgets = {}

        self.user_id_label = QLabel("user_id: loading...")
        self.user_hash_label = QLabel("user_hash: loading...")
        self.balance_label = QLabel("Баланс: loading...")
        
        self.bot_manager = BotManager()

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
        layout.addWidget(self.add_bot_button)
        layout.addWidget(QLabel("Список ботов:"))
        layout.addWidget(self.bot_list)
        layout.addWidget(self.signal_log)
        self.setLayout(layout)

        QTimer.singleShot(0, self.start_async_tasks)

    def start_async_tasks(self):
        from core import ws_client
        ws_client.signal_log_callback = self.append_to_log
        asyncio.create_task(self._init_session_and_loop())
        asyncio.create_task(listen_to_signals())

    async def _init_session_and_loop(self):
        # 1. Получаем сессию и учётные данные один раз
        session = await asyncio.to_thread(create_session_from_browser_cookies)
        if not session:
            self.user_id_label.setText("Ошибка: нет сессии")
            self.user_hash_label.setText("")
            self.balance_label.setText("Баланс: ошибка")
            return

        user_id, user_hash = await asyncio.to_thread(extract_user_credentials, session)
        if not user_id or not user_hash:
            self.user_id_label.setText("Ошибка: нет user_id")
            self.user_hash_label.setText("")
            self.balance_label.setText("Баланс: ошибка")
            return

        # 2. Сохраняем в поля
        self.session = session
        self.user_id = user_id
        self.user_hash = user_hash

        # 3. Отображаем
        self.user_id_label.setText(f"user_id: {user_id}")
        self.user_hash_label.setText(f"user_hash: {user_hash}")

        # 4. Запускаем цикл обновления баланса
        await self._update_balance_loop()

    async def _update_balance_loop(self):
        while True:
            try:
                balance = get_balance(
                    self.session, self.user_id, self.user_hash
                )
                self.balance_label.setText(f"Баланс: {balance:.2f} руб.")
            except Exception as e:
                print(f"[!] Ошибка при получении баланса: {e}")
                self.balance_label.setText("Баланс: ошибка")
            await asyncio.sleep(5)

    def append_to_log(self, text: str):
        self.signal_log.append(str(text))


    def show_add_bot_dialog(self):
        dialog = AddBotDialog(available_symbols=self.available_symbols,
                              available_strategies=self.available_strategies)
        if dialog.exec():
            strategy_name = dialog.selected_strategy
            symbol = dialog.selected_symbol

            if not all([self.session, self.user_id, self.user_hash]):
                self.append_to_log("❌ Сессия не готова. Подождите...")
                return

            strategy_class = {
                "martingale": MartingaleStrategy,
                # "OscarGrind": OscarGrindStrategy,  # Добавишь потом
            }.get(strategy_name)

            if not strategy_class:
                self.append_to_log(f"❌ Стратегия '{strategy_name}' не найдена.")
                return

            # Создаём стратегию
            strategy = strategy_class(
                session=self.session,
                user_id=self.user_id,
                user_hash=self.user_hash,
                symbol=symbol,
                log_callback=self.append_to_log
            )

            # Добавляем и запускаем
            bot = Bot(strategy_cls=strategy_class,
                    strategy_kwargs={
                        "session": self.session,
                        "user_id": self.user_id,
                        "user_hash": self.user_hash,
                        "symbol": symbol,
                        "log_callback": self.append_to_log,
                    },
                    on_log=self.append_to_log,
                    on_finish=lambda: self.on_bot_finished(bot)
                    )
            self.bot_manager.add_bot(bot)
            # Добавляем в QListWidget
            from gui.bot_item_widget import BotItemWidget
            from gui.settings_factory import get_settings_dialog_cls

            item = QListWidgetItem()
            title = f"{strategy_name} [{symbol}]"
            widget = BotItemWidget(
                title=title,
                on_settings=partial(self.open_settings_dialog, bot),
                on_pause_resume=lambda paused, b=bot: self.toggle_pause(b, paused),
                on_stop=partial(self.stop_bot, bot)
            )
            item.setSizeHint(widget.sizeHint())
            self.bot_list.addItem(item)
            self.bot_list.setItemWidget(item, widget)

            self.bot_items[bot] = item
            self.bot_widgets[bot] = widget
            
    def on_bot_finished(self, bot):
        # убрать из UI
        item = self.bot_items.pop(bot, None)
        widget = self.bot_widgets.pop(bot, None)
        if item is not None:
            row = self.bot_list.row(item)
            self.bot_list.takeItem(row)
        # убрать из менеджера (если ещё там)
        try:
            self.bot_manager.remove_bot(bot)
        except Exception:
            pass
        self.append_to_log(f"ℹ️ Бот завершил работу: {bot.strategy_cls.__name__} [{bot.strategy_kwargs.get('symbol')}]")

    def stop_bot(self, bot):
        bot.stop()
        self.on_bot_finished(bot)

    def toggle_pause(self, bot, paused: bool):
        if paused:
            bot.pause()
            self.append_to_log(f"⏸ Пауза: {bot.strategy_cls.__name__} [{bot.strategy_kwargs.get('symbol')}]")
        else:
            bot.resume()
            self.append_to_log(f"▶ Продолжить: {bot.strategy_cls.__name__} [{bot.strategy_kwargs.get('symbol')}]")

    def open_settings_dialog(self, bot):
        from gui.settings_factory import get_settings_dialog_cls
        dlg_cls = get_settings_dialog_cls(bot.strategy_cls)
        if not dlg_cls:
            self.append_to_log("⚠ Нет окна настроек для этой стратегии.")
            return

        # Текущие параметры стратегии
        current = dict(bot._strategy.params) if bot._strategy else dict(bot.strategy_kwargs)

        dlg = dlg_cls(current, parent=self)
        if dlg.exec():
            new_params = dlg.get_params()
            # обновляем и перезапускаем бота с новыми параметрами:
            self.append_to_log(f"⚙ Обновление настроек: {new_params}")
            self.stop_bot(bot)
            # создаём новый Bot с тем же классом, но обновлёнными params
            strategy_kwargs = dict(bot.strategy_kwargs)
            strategy_kwargs.update(new_params)
            new_bot = Bot(
                strategy_cls=bot.strategy_cls,
                strategy_kwargs=strategy_kwargs,
                on_log=self.append_to_log,
                on_finish=lambda b=None: self.on_bot_finished(b or new_bot)
            )
            self.bot_manager.add_bot(new_bot)

            # заменим виджет/элемент списка на новый bot
            item = self.bot_items.pop(bot, None)
            widget = self.bot_widgets.pop(bot, None)
            if item is not None:
                self.bot_items[new_bot] = item
                self.bot_widgets[new_bot] = widget
                # Обновим заголовок (если нужно)
                widget.label.setText(f"{bot.strategy_cls.__name__} [{strategy_kwargs.get('symbol')}]")


    def _on_item_double_clicked(self, item):
        # найдём bot по item
        for bot, it in self.bot_items.items():
            if it is item:
                self.open_settings_dialog(bot)
                break
