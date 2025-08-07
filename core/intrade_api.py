import asyncio
import requests
from bs4 import BeautifulSoup
from PyQt6.QtCore import qDebug

BALANCE_URL = "https://intrade27.bar/balance.php"
TRADE_URL = "https://intrade27.bar/ajax5_new.php"
TRADE_CHECK_URL = "https://intrade27.bar/trade_check2.php"
PERCENT_URL = "https://intrade27.bar/ajax_percent.php"

def get_balance(session, user_id, user_hash):
    payload = {"user_id": user_id, "user_hash": user_hash}
    r = session.post(BALANCE_URL, data=payload)
    if not r.ok:
        return 0.0
    text = r.text.strip().replace(" ", "").replace(",", ".").replace("₽", "").replace("$", "")
    try:
        return float(text)
    except:
        return 0.0
        

def get_current_percent(session, investment, option):
    payload = {
        "type": "Sprint",
        "time": "1",
        "currency_name": "RUB",
        "investment": str(investment),
        "percent": "",
        "option": option
    }
    r = session.post(PERCENT_URL, data=payload)
    try:
        return int(r.text.strip())
    except:
        return None

def place_trade(session, user_id, user_hash, investment, option, status):
    payload = {
        "user_id": user_id,
        "user_hash": user_hash,
        "option": option,
        "investment": str(investment),
        "time": "1",
        "date": "0",
        "trade_type": "Sprint",
        "status": status,
    }
    r = session.post(TRADE_URL, data=payload)
    soup = BeautifulSoup(r.text, "html.parser")
    trade = soup.find("tr", class_="trade_graph_tick")
    if trade and trade.has_attr("data-id"):
        return trade["data-id"]
    return None

async def check_trade_result(session, user_id, user_hash, trade_id, wait_time=62):
    await asyncio.sleep(wait_time)
    payload = {"user_id": user_id, "user_hash": user_hash, "trade_id": trade_id}
    r = session.post(TRADE_CHECK_URL, data=payload)
    parts = r.text.strip().split(";")
    if len(parts) >= 3:
        try:
            rate, result, investment = parts
            return float(result) - float(investment)
        except:
            return None
    return None

