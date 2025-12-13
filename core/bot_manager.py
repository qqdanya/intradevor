"""Управление жизненным циклом всех запущенных ботов."""

from __future__ import annotations

import asyncio
from typing import Iterable, List, Optional, Type

from core.bot import Bot


class BotManager:
    """Коллекция всех активных ботов."""

    def __init__(self) -> None:
        self.bots: List[Bot] = []

    def add_bot(self, bot: Bot) -> None:
        """Добавить бота в менеджер."""
        self.bots.append(bot)

    def remove_bot(self, bot: Bot) -> None:
        """Остановить и удалить бота из менеджера."""
        if bot in self.bots:
            bot.stop()
            self.bots.remove(bot)

    def get_all_bots(self) -> Iterable[Bot]:
        """Вернуть итератор по всем зарегистрированным ботам."""
        return list(self.bots)

    def stop_all(self) -> None:
        """Остановить всех ботов."""
        loop = asyncio.get_running_loop()
        tasks = []
        for bot in list(self.bots):
            tasks.append(loop.create_task(bot.stop_and_wait()))
        self.bots.clear()
        if tasks:
            loop.create_task(asyncio.gather(*tasks, return_exceptions=True))

    def find_by_symbol_and_strategy(
        self, symbol: str, strategy_cls: Type
    ) -> Optional[Bot]:
        """Найти бота по символу и классу стратегии."""
        for bot in self.bots:
            if (
                bot.strategy_kwargs.get("symbol") == symbol
                and bot.strategy_cls == strategy_cls
            ):
                return bot
        return None
