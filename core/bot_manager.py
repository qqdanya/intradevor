from core.bot import Bot


class BotManager:
    def __init__(self):
        self.bots = []

    def add_bot(self, bot: Bot):
        self.bots.append(bot)

    def remove_bot(self, bot: Bot):
        if bot in self.bots:
            bot.stop()
            self.bots.remove(bot)

    def get_all_bots(self):
        return self.bots

    def stop_all(self):
        for bot in self.bots:
            bot.stop()

    def find_by_symbol_and_strategy(self, symbol: str, strategy_cls):
        for bot in self.bots:
            if (bot.strategy_kwargs.get("symbol") == symbol
                    and bot.strategy_cls == strategy_cls):
                return bot
        return None

