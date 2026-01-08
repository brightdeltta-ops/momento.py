import json
import websocket
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
CURRENCY = "USD"

STAKE = 200
DURATION = 1
DURATION_UNIT = "m"

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_LOOKBACK = 2

COOLDOWN_SECONDS = 60        # 1 trade per candle
MAX_HISTORY = 300

MAX_CONSECUTIVE_LOSSES = 3  # LOSS BREAKER
LOSS_COOLDOWN = 900         # 15 minutes pause

# =========================
# STATE
# =========================
price_history = deque(maxlen=MAX_HISTORY)
in_trade = False
last_trade_time = 0
ws = None
balance = 0

loss_streak = 0
breaker_active = False
breaker_until = 0

tick_count = 0
last_heartbeat = 0

# =========================
# UTIL
# =========================
def ts():
    return datetime.now().strftime("%H:%M:%S")

# =========================
# INDICATORS
# =========================
def ema(prices, period):
    if len(prices) < period:
        return None
    prices = np.array(prices)
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices, weights, mode="valid")[-1]

def detect_fractal(prices):
    L = FRACTAL_LOOKBACK
    if len(prices) < (L * 2 + 1):
        return None

    mid = prices[-L - 1]
    left = prices[-(2 * L + 1):-L - 1]
    right = prices[-L:]

    if mid > max(left + right):
        return "UP"
    if mid < min(left + right):
        return "DOWN"
    return None

# =========================
# TRADE ENGINE
# =========================
def try_trade():
    global in_trade, last_trade_time, breaker_active

    now = time.time()

    if breaker_active:
        if now < breaker_until:
            return
        breaker_active = False
        print(f"{ts()} 🟢 Loss breaker released")

    if in_trade:
        return

    if now - last_trade_time < COOLDOWN_SECONDS:
        return

    prices = list(price_history)
    fast = ema(prices, EMA_FAST)
    slow = ema(prices, EMA_SLOW)
    fractal = detect_fractal(prices)

    if fast is None or slow is None or fractal is None:
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
            "currency": CURRENCY,
            "duration": DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol": SYMBOL
        }
    }
    ws.send(json.dumps(proposal))
    print(f"{ts()} 📨 Proposal {direction}")

# =========================
# WEBSOCKET CALLBACKS
# =========================
def on_message(ws, message):
    global in_trade, balance, loss_streak, breaker_active, breaker_until
    global tick_count, last_heartbeat

    data = json.loads(message)

    if "authorize" in data:
        ws.send(json.dumps({
            "ticks": SYMBOL,
            "subscribe": 1
        }))
        print(f"{ts()} 📡 Subscribed to ticks {SYMBOL}")

    elif "tick" in data:
        price = float(data["tick"]["quote"])
        price_history.append(price)
        tick_count += 1

        now = time.time()
        if now - last_heartbeat > 10:
            print(f"{ts()} ❤️ Heartbeat | Ticks {tick_count}")
            last_heartbeat = now

        try_trade()

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]

        if poc.get("is_sold"):
            profit = poc["profit"]
            balance = poc["balance_after"]

            if profit < 0:
                loss_streak += 1
                print(f"{ts()} ❌ Loss streak {loss_streak}")
            else:
                loss_streak = 0

            print(f"{ts()} ✔ Settled | P/L {profit:.2f} | Bal {balance:.2f}")
            in_trade = False

            if loss_streak >= MAX_CONSECUTIVE_LOSSES:
                breaker_active = True
                breaker_until = time.time() + LOSS_COOLDOWN
                print(f"{ts()} 🔴 LOSS BREAKER ACTIVATED ({LOSS_COOLDOWN//60} min pause)")

def on_open(ws):
    print(f"{ts()} ✅ Connected")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, error):
    print(f"{ts()} ❌ WS Error {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"{ts()} ❌ WS Closed ({close_status_code}) — reconnecting")
    time.sleep(3)
    start_ws()

# =========================
# START WS
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
    ws.run_forever(ping_interval=30, ping_timeout=10)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    start_ws()