from core.intrade_api import get_current_percent, place_trade, check_trade_result
from core.signal_waiter import wait_for_signal


async def oscar_grind_strategy(
    session, user_id, user_hash, base_investment=100, symbol="EURUSD"
):
    total_profit = 0.0
    investment = base_investment
    trade_number = 1

    while total_profit < base_investment:
        percent = get_current_percent(session, investment, symbol)
        if percent is None:
            continue

        status = await wait_for_signal(symbol)
        trade_id = place_trade(session, user_id, user_hash, investment, symbol, status)
        if not trade_id:
            continue

        profit = await check_trade_result(session, user_id, user_hash, trade_id)
        if profit is None:
            continue

        print(
            f"[{symbol}] Сделка {trade_number}: {'ПРИБЫЛЬ' if profit > 0 else 'УБЫТОК'} {profit:.2f}"
        )
        trade_number += 1
        total_profit += profit

        if profit > 0:
            remaining = base_investment - total_profit
            investment = max(100, remaining / (percent / 100.0))
        else:
            investment = min(investment + 100, 50000)
