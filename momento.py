import json
import time
import threading
import websocket
import os
from collections import deque

# ===================== CONFIG =====================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))

SYMBOL = "R_10"          # V10 sniper
BASE_STAKE = 200.0
MAX_STAKE = 500.0
TRADE_DURATION = 1       # ticks
COOLDOWN = 5             # seconds between trades

EMA_FAST = 9
EMA_SLOW = 21

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ===================== STATE =====================
ticks = deque(maxlen=200)

BALANCE = 0.0
STAKE = BASE_STAKE
WINS = 0
LOSSES = 0
TRADES = 0

trade_open = False
last_trade_time = 0
active_contract_id = None

ws = None

# ===================== LOG =====================
def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

# ===================== EMA =====================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

# ===================== STRATEGY =====================
def get_signal():
    if len(ticks) < EMA_SLOW + 3:
        return None

    price = ticks[-1]
    ef = ema(list(ticks)[-EMA_FAST:], EMA_FAST)
    es = ema(list(ticks)[-EMA_SLOW:], EMA_SLOW)

    if ef is None or es is None:
        return None

    if ef > es and price > ef:
        return "CALL"
    if ef < es and price < ef:
        return "PUT"

    return None

# ===================== TRADE =====================
def send_proposal(direction):
    global trade_open, last_trade_time

    proposal = {
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": TRADE_DURATION,
        "duration_unit": "t",
        "symbol": SYMBOL
    }

    trade_open = True
    last_trade_time = time.time()
    ws.send(json.dumps(proposal))

    log(f"🎯 {SYMBOL} {direction} | STAKE {STAKE:.2f}")

# ===================== WATCHDOG =====================
def trade_watchdog():
    global trade_open
    while True:
        time.sleep(1)
        if trade_open and time.time() - last_trade_time > 8:
            log("⚠ FORCE RELEASE — NO SETTLEMENT")
            trade_open = False

# ===================== SETTLEMENT =====================
def settle(contract):
    global BALANCE, STAKE, WINS, LOSSES, TRADES, trade_open, active_contract_id

    profit = float(contract.get("profit", 0))
    BALANCE += profit
    TRADES += 1

    if profit > 0:
        WINS += 1
        STAKE = min(STAKE * 1.15, MAX_STAKE)
        log(f"✔ WIN {profit:.2f} → {STAKE:.2f}")
    else:
        LOSSES += 1
        STAKE = BASE_STAKE
        log(f"❌ LOSS {profit:.2f} → RESET")

    trade_open = False
    active_contract_id = None

# ===================== WS HANDLER =====================
def on_message(ws, msg):
    global BALANCE, active_contract_id

    data = json.loads(msg)

    if data.get("msg_type") == "authorize":
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("🔐 AUTHORIZED")

    elif data.get("msg_type") == "tick":
        price = float(data["tick"]["quote"])
        ticks.append(price)

        if not trade_open and time.time() - last_trade_time > COOLDOWN:
            signal = get_signal()
            if signal:
                send_proposal(signal)

    elif data.get("msg_type") == "proposal":
        pid = data["proposal"]["id"]
        ws.send(json.dumps({"buy": pid, "price": STAKE}))

    elif data.get("msg_type") == "buy":
        active_contract_id = data["buy"]["contract_id"]
        log(f"🟢 BUY CONFIRMED {active_contract_id}")

    elif data.get("msg_type") == "proposal_open_contract":
        c = data["proposal_open_contract"]
        if c.get("contract_id") == active_contract_id and c.get("is_sold"):
            settle(c)

    elif data.get("msg_type") == "balance":
        BALANCE = float(data["balance"]["balance"])

def on_open(ws):
    log("🌐 CONNECTED")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, err):
    log(f"❌ WS ERROR: {err}")

def on_close(ws, *args):
    log("❌ WS CLOSED")

# ===================== START =====================
def start():
    global ws
    log("🚀 V10 CLOUD BOT — LIVE")

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    threading.Thread(target=trade_watchdog, daemon=True).start()
    ws.run_forever()

if __name__ == "__main__":
    start()