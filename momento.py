# =====================================
# MOMENTO — MONTE CARLO ALPHA (KOYEB)
# =====================================

import os
import json
import time
import math
import websocket
import threading
from collections import deque
from datetime import datetime, timezone

import numpy as np

# ============== ENV ==================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))
SYMBOL = os.getenv("SYMBOL", "R_75")
BASE_STAKE = float(os.getenv("BASE_STAKE", "100"))

# ============== PARAMETERS ===========
EMA_FAST = 5
EMA_SLOW = 21
MICRO_LEN = 30

CONF_THRESHOLD = 0.25
COOLDOWN = 2.0
MAX_EDGE = 0.25

# ============== STATE ================
ticks = deque(maxlen=MICRO_LEN)
balance = 0.0
wins = 0
losses = 0
edge = 0.0
last_trade_ts = 0
ws = None

# ============== UTILS =================
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

# ============== CORE LOGIC ============
def detect_confidence():
    if len(ticks) < EMA_SLOW:
        return None, 0.0

    ef = ema(ticks, EMA_FAST)
    es = ema(ticks, EMA_SLOW)
    if ef is None or es is None:
        return None, 0.0

    diff = abs(ef - es)
    vol = np.std(ticks)

    if vol == 0:
        return None, 0.0

    confidence = min(1.0, diff / (vol * 0.6))
    direction = "CALL" if ef > es else "PUT"

    return direction, confidence

def maybe_trade():
    global last_trade_ts

    now = time.time()
    if now - last_trade_ts < COOLDOWN:
        return

    direction, confidence = detect_confidence()
    if not direction or confidence < CONF_THRESHOLD:
        return

    stake = round(BASE_STAKE * (1 + edge), 2)
    send_trade(direction, stake)
    last_trade_ts = now

def send_trade(direction, stake):
    log(f"🔥 TRADE {direction} | stake={stake}")

    ws.send(json.dumps({
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": direction,
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",
            "symbol": SYMBOL
        }
    }))

# ============== WS HANDLERS ===========
def on_message(wsapp, message):
    global balance, wins, losses, edge

    data = json.loads(message)

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        maybe_trade()

    elif "balance" in data:
        balance = float(data["balance"]["balance"])

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            pl = float(poc.get("profit", 0))
            if pl > 0:
                wins += 1
                edge = min(MAX_EDGE, edge + 0.03)
            else:
                losses += 1
                edge = max(0.0, edge - 0.02)

def on_open(wsapp):
    log("AUTHORIZED")
    wsapp.send(json.dumps({"authorize": API_TOKEN}))
    wsapp.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    wsapp.send(json.dumps({"balance": 1, "subscribe": 1}))

def on_error(wsapp, error):
    log(f"ERROR {error}")

def on_close(wsapp):
    log("WS CLOSED")

# ============== HEARTBEAT ============
def heartbeat():
    while True:
        log(f"❤️ BAL={balance:.2f} W={wins} L={losses} EDGE={edge:.2f}")
        time.sleep(30)

# ============== MAIN ==================
if __name__ == "__main__":
    log("🚀 MONTE-CARLO ALPHA BOT — KOYEB READY")

    threading.Thread(target=heartbeat, daemon=True).start()

    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever(ping_interval=20, ping_timeout=10)