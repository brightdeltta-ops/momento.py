# =====================================
# MOMENTO — HEADLESS FIRING BOT (KOYEB)
# =====================================

import os, json, time, threading, websocket
import numpy as np
from collections import deque
from datetime import datetime, timezone

# =============== ENV ==================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))
SYMBOL = os.getenv("SYMBOL", "R_75")

BASE_STAKE = float(os.getenv("BASE_STAKE", "1"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "5"))

# ============ PARAMETERS ==============
EMA_FAST = 5
EMA_SLOW = 21
WINDOW = 40

CONF_THRESHOLD = 0.65
COOLDOWN = 20        # seconds between trades
MAX_EDGE = 0.30

# =============== STATE ================
ticks = deque(maxlen=WINDOW)
balance = 0.0
wins = losses = 0
edge = 0.0

trade_in_progress = False
last_trade_ts = 0
pending_buy = None

ws = None

# =============== UTILS =================
def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

# ============ STRATEGY =================
def detect_signal():
    if len(ticks) < EMA_SLOW:
        return None, 0.0

    prices = list(ticks)
    ef = ema(prices[-EMA_FAST:], EMA_FAST)
    es = ema(prices[-EMA_SLOW:], EMA_SLOW)

    if ef is None or es is None:
        return None, 0.0

    vol = np.std(prices)
    if vol <= 0:
        return None, 0.0

    diff = abs(ef - es)
    conf = min(1.0, diff / (vol * 0.6))

    direction = "CALL" if ef > es else "PUT"
    return direction, conf

# ============ EXECUTION ================
def maybe_trade():
    global last_trade_ts, trade_in_progress

    if trade_in_progress:
        return

    if time.time() - last_trade_ts < COOLDOWN:
        return

    direction, conf = detect_signal()
    if not direction or conf < CONF_THRESHOLD:
        return

    stake = round(min(BASE_STAKE * (1 + edge), MAX_STAKE), 2)
    request_proposal(direction, stake)
    last_trade_ts = time.time()
    trade_in_progress = True

def request_proposal(direction, stake):
    log(f"📨 PROPOSAL {direction} | stake={stake}")
    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

def send_buy(pid, stake):
    log(f"🔥 BUY SENT | stake={stake}")
    ws.send(json.dumps({
        "buy": pid,
        "price": stake
    }))

# ============ WS HANDLERS ==============
def on_message(wsapp, message):
    global balance, wins, losses, edge, trade_in_progress

    data = json.loads(message)

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        maybe_trade()

    elif "balance" in data:
        balance = float(data["balance"]["balance"])

    elif "proposal" in data:
        pid = data["proposal"]["id"]
        stake = float(data["proposal"]["ask_price"])
        send_buy(pid, stake)

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0))
            if profit > 0:
                wins += 1
                edge = min(MAX_EDGE, edge + 0.05)
            else:
                losses += 1
                edge = max(0.0, edge - 0.03)

            log(f"✔ SETTLED | P/L={profit:.2f} | EDGE={edge:.2f}")
            trade_in_progress = False

def on_open(wsapp):
    log("CONNECTED & AUTHORIZED")
    wsapp.send(json.dumps({"authorize": API_TOKEN}))
    wsapp.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    wsapp.send(json.dumps({"balance": 1, "subscribe": 1}))
    wsapp.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def on_error(wsapp, error):
    log(f"WS ERROR: {error}")

def on_close(wsapp, *args):
    log("WS CLOSED — reconnecting")

# ============ HEARTBEAT ================
def heartbeat():
    while True:
        log(f"❤️ BAL={balance:.2f} W={wins} L={losses} EDGE={edge:.2f}")
        time.sleep(30)

# =============== MAIN ==================
if __name__ == "__main__":
    log("🚀 MOMENTO — FIRING BOT (KOYEB READY)")

    threading.Thread(target=heartbeat, daemon=True).start()

    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever(ping_interval=20, ping_timeout=10)