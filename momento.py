import json
import websocket
import threading
import time
import os
from collections import deque
import numpy as np
from datetime import datetime

# =========================
# CONFIG
# =========================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))
SYMBOL = "frxUSDJPY"
STAKE = 200
DURATION = 1
DURATION_UNIT = "m"

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_LOOKBACK = 2
COOLDOWN_SECONDS = 5
MAX_HISTORY = 200

# =========================
# STATE
# =========================
price_history = deque(maxlen=MAX_HISTORY)
in_trade = False
last_trade_time = 0
ws = None
balance = 0

# =========================
# INDICATORS
# =========================
def ema(prices, period):
    if len(prices) < period:
        return None
    prices = np.array(prices)
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices, weights, mode='valid')[-1]

def detect_fractal(prices):
    prices = list(prices)
    if len(prices) < FRACTAL_LOOKBACK * 2 + 1:
        return None

    mid = prices[-FRACTAL_LOOKBACK-1]
    left = prices[-(FRACTAL_LOOKBACK*2+1):-FRACTAL_LOOKBACK-1]
    right = prices[-FRACTAL_LOOKBACK:]

    if mid > max(left + right):
        return "UP"
    if mid < min(left + right):
        return "DOWN"
    return None

# =========================
# TRADE LOGIC
# =========================
def try_trade():
    global in_trade, last_trade_time

    if in_trade:
        return

    now = time.time()
    if now - last_trade_time < COOLDOWN_SECONDS:
        return

    prices = list(price_history)
    fast = ema(prices, EMA_FAST)
    slow = ema(prices, EMA_SLOW)
    fractal = detect_fractal(prices)

    if not fast or not slow or not fractal:
        return

    direction = None

    if fractal == "UP" and fast > slow:
        direction = "CALL"
    elif fractal == "DOWN" and fast < slow:
        direction = "PUT"

    if not direction:
        return

    print(f"{ts()} 🎯 FRACTAL+EMA → {direction}")

    send_trade(direction)
    in_trade = True
    last_trade_time = now

def send_trade(direction):
    proposal = {
        "buy": 1,
        "price": STAKE,
        "parameters": {
            "amount": STAKE,
            "basis": "stake",
            "contract_type": direction,
            "currency": "USD",
            "duration": DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol": SYMBOL
        }
    }
    ws.send(json.dumps(proposal))
    print(f"{ts()} 📨 Proposal {direction}")

# =========================
# WEBSOCKET HANDLERS
# =========================
def on_message(ws, message):
    global in_trade, balance
    data = json.loads(message)

    if "tick" in data:
        price = float(data["tick"]["quote"])
        price_history.append(price)
        try_trade()

    elif "buy" in data:
        pass

    elif "proposal_open_contract" in data:
        contract = data["proposal_open_contract"]
        if contract.get("is_sold"):
            profit = contract["profit"]
            balance = contract["balance_after"]
            print(f"{ts()} ✔ Settled | P/L {profit:.2f} | Bal {balance:.2f}")
            in_trade = False

def on_open(ws):
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, error):
    print(f"{ts()} ❌ WS Error {error}")

def on_close(ws):
    print(f"{ts()} ❌ WS Closed — reconnecting")
    time.sleep(2)
    start_ws()

# =========================
# START
# =========================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

def ts():
    return datetime.now().strftime("%H:%M:%S")

if __name__ == "__main__":
    start_ws()