# gui/main_window.py
from PyQt6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QTextEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QDialog,
    QFormLayout,
    QSpinBox,
    QDialogButtonBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
)
from PyQt6.QtGui import QTextCursor, QColor, QBrush
from PyQt6.QtCore import Qt, QTimer
from collections import defaultdict
from functools import partial
import asyncio

from core.symbols import ui_symbol
from core.money import format_money
from core.logger import ts

from gui.bot_add_dialog import AddBotDialog
from gui.risk_dialog import RiskDialog
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


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

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
        }
        self.strategy_labels = {
            "martingale": "Мартингейл",
        }

        self.bot_items = {}
        self.bot_widgets = {}
        self.bot_logs = defaultdict(list)
        self.bot_log_listeners = defaultdict(list)
        self.pending_trades = {}

        self.user_id_label = QLabel("user_id: loading...")
        self.user_hash_label = QLabel("user_hash: loading...")
        self.balance_label = QLabel("Баланс: loading...")

        self.bot_manager = BotManager()

        self.change_currency_button = QPushButton("Сменить валюту")
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

        self.bot_list = QListWidget()
        self.bot_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.bot_list.itemDoubleClicked.connect(self._on_item_double_clicked)

        self.signal_log = QTextEdit()
        self.signal_log.setReadOnly(True)

        # === Таблица результатов сделок ===
        self.trades_table = QTableWidget(self)
        self.trades_table.setColumnCount(8)
        self.trades_table.setHorizontalHeaderLabels(
            [
                "Время",
                "Валютная пара",
                "ТФ",
                "Направление",
                "Ставка",
                "Процент",
                "P/L",
                "Счет",
            ]
        )
        hdr = self.trades_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # Пара
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # ТФ
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # Время
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)  # Напр.
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # Ставка
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # %
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)  # P/L
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)  # Счет
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.setSortingEnabled(True)

        layout = QVBoxLayout()
        layout.addWidget(self.user_id_label)
        layout.addWidget(self.user_hash_label)
        layout.addWidget(self.balance_label)
        layout.addWidget(self.change_currency_button)
        layout.addWidget(self.set_risk_button)
        layout.addWidget(self.toggle_demo_button)
        layout.addWidget(self.add_bot_button)
        layout.addWidget(QLabel("Список ботов:"))
        layout.addWidget(self.bot_list)
        layout.addWidget(QLabel("Логи:"))
        layout.addWidget(self.signal_log)
        layout.addWidget(QLabel("Сделки:"))
        layout.addWidget(self.trades_table)
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
                self.append_to_log(f"[!] Ошибка при получении баланса: {e}")
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
        s = ts(str(text)) + "\n"
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
                        "account_currency": getattr(self, "account_currency", "RUB"),
                        "on_trade_result": self.add_trade_result,
                        "on_trade_pending": self.add_trade_pending,
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
        self.on_bot_finished(bot)

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

    def _open_risk_dialog(self):
        res = RiskDialog.get_values(self, min_default=1, max_default=100)
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
        placed_at: str,
        symbol: str,
        timeframe: str,
        direction: int,
        stake: float,
        percent: int,
        wait_seconds: float,
    ):
        """
        Добавляет строку «ожидание результата»:
          P/L -> "Ожидание (чч:мм:сс/мм:сс/с)", строка жёлтая.
        Таймер раз в секунду обновляет обратный отсчёт.
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

        def _do():
            was_sorting = self.trades_table.isSortingEnabled()
            if was_sorting:
                self.trades_table.setSortingEnabled(False)

            row = 0
            self.trades_table.insertRow(row)

            dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
            remaining_txt = _fmt_left(wait_seconds)
            account_txt = "ДЕМО" if getattr(self, "is_demo", False) else "РЕАЛ"

            vals = [
                placed_at,  # 0 Время
                symbol,  # 1 Пара
                timeframe,  # 2 ТФ
                dir_text,  # 3 Направление
                f"{stake:.2f}",  # 4 Ставка
                f"{percent}%",  # 5 %
                f"Ожидание ({remaining_txt})",  # 6 P/L
                account_txt,  # 7 Счёт
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
                # если строка пропала — стоп
                if row >= self.trades_table.rowCount():
                    timer.stop()
                    return
                item = self.trades_table.item(row, 6)  # P/L
                if item:
                    item.setText(f"Ожидание ({_fmt_left(left)})")
                if left <= 0:
                    timer.stop()

            timer.timeout.connect(_tick)
            timer.start()

            prev = self.pending_trades.get(trade_id)
            if prev and isinstance(prev.get("timer"), QTimer):
                try:
                    prev["timer"].stop()
                except Exception:
                    pass
            self.pending_trades[trade_id] = {"row": row, "timer": timer}

            if was_sorting:
                self.trades_table.setSortingEnabled(True)
            self.trades_table.scrollToTop()

        QTimer.singleShot(0, _do)

    def add_trade_result(
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
    ):
        """
        Если есть pending с таким trade_id — обновляем его строку.
        Иначе добавляем новую. Красим:
          >0 — зелёный, <0 — красный, ==0 или None — серый.
        """

        def _fill_row(row: int):
            dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
            account_txt = "ДЕМО" if getattr(self, "is_demo", False) else "РЕАЛ"

            vals = [
                placed_at,  # 0
                symbol,  # 1
                timeframe,  # 2
                dir_text,  # 3
                f"{stake:.2f}",  # 4
                f"{percent}%",  # 5
                f"{profit:.2f}" if profit is not None else "—",  # 6
                account_txt,  # 7
            ]
            for col, v in enumerate(vals):
                self.trades_table.setItem(row, col, QTableWidgetItem(str(v)))

            if profit is None or abs(profit) < 1e-9:
                color = QColor("#e0e0e0")  # серый (PUSH/неизвестно)
            elif profit > 0:
                color = QColor("#d1f7c4")  # зелёный
            else:
                color = QColor("#ffd6d6")  # красный
            brush = QBrush(color)
            for c in range(self.trades_table.columnCount()):
                it = self.trades_table.item(row, c)
                if it:
                    it.setBackground(brush)

        def _do():
            was_sorting = self.trades_table.isSortingEnabled()
            if was_sorting:
                self.trades_table.setSortingEnabled(False)

            row_to_update = None
            if trade_id and trade_id in self.pending_trades:
                info = self.pending_trades.pop(trade_id, {})
                timer = info.get("timer")
                if isinstance(timer, QTimer):
                    try:
                        timer.stop()
                    except Exception:
                        pass
                row = info.get("row")
                if isinstance(row, int) and 0 <= row < self.trades_table.rowCount():
                    row_to_update = row

            if row_to_update is None:
                row_to_update = 0
                self.trades_table.insertRow(row_to_update)

            _fill_row(row_to_update)

            if was_sorting:
                self.trades_table.setSortingEnabled(True)
            self.trades_table.scrollToTop()

        QTimer.singleShot(0, _do)
