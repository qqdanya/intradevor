# gui/main_window.py
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import QTimer, qDebug
import asyncio

# Импортируем твой модуль, где лежат функции
from core.session import create_session_from_browser_cookies, extract_user_credentials
from core.intrade_api import get_balance


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.session = None
        self.user_id = None
        self.user_hash = None

        self.user_id_label = QLabel("user_id: loading...")
        self.user_hash_label = QLabel("user_hash: loading...")
        self.balance_label = QLabel("Баланс: loading...")

        layout = QVBoxLayout()
        layout.addWidget(self.user_id_label)
        layout.addWidget(self.user_hash_label)
        layout.addWidget(self.balance_label)
        self.setLayout(layout)

        QTimer.singleShot(0, self.start_async_tasks)

    def start_async_tasks(self):
        asyncio.create_task(self._init_session_and_loop())

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
