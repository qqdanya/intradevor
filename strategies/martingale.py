from core.intrade_api import get_current_percent, place_trade, check_trade_result
from core.signal_waiter import wait_for_signal

async def martingale_strategy(session, user_id, user_hash, base_investment=100, symbol="EURUSD", max_steps=5):
    investment = base_investment
    step = 0

    while step < max_steps:
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

        if profit > 0:
            print(f"[{symbol}] ✅ Профит: {profit:.2f}. Сброс.")
            investment = base_investment
            step = 0
        else:
            print(f"[{symbol}] ❌ Убыток: {profit:.2f}. Удвоение.")
            investment = min(investment * 2, 50000)
            step += 1

