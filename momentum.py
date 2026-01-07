import os
import json
import time
import websocket
import threading
from collections import deque
from datetime import datetime

# ================== CONFIG ==================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Your Deriv API token
APP_ID = int(os.getenv("APP_ID", "0"))    # Your app ID
SYMBOL = "frxUSDJPY"                      # USD/JPY Deriv symbol

# Risk management
MAX_OPEN_TRADES = 3
DAILY_DRAWDOWN_LIMIT = 200  # adjust per account
STAKE_PER_TRADE = 1.0       # adjust per account

# Ultra Elite EMA-Fractal setups (example placeholders)
ULTRA_ELITE_SETUPS = [
    {"id": 1, "ema_short": 5, "ema_long": 20, "fractal_lookback": 3, "threshold": 0.5},
    {"id": 2, "ema_short": 10, "ema_long": 50, "fractal_lookback": 5, "threshold": 0.8},
    {"id": 3, "ema_short": 8, "ema_long": 30, "fractal_lookback": 4, "threshold": 0.6},
    {"id": 4, "ema_short": 12, "ema_long": 60, "fractal_lookback": 6, "threshold": 1.0},
    {"id": 5, "ema_short": 7, "ema_long": 25, "fractal_lookback": 3, "threshold": 0.7},
    {"id": 6, "ema_short": 9, "ema_long": 40, "fractal_lookback": 4, "threshold": 0.5},
    {"id": 7, "ema_short": 15, "ema_long": 50, "fractal_lookback": 5, "threshold": 0.9},
    {"id": 8, "ema_short": 6, "ema_long": 20, "fractal_lookback": 3, "threshold": 0.4},
    {"id": 9, "ema_short": 11, "ema_long": 45, "fractal_lookback": 4, "threshold": 0.6},
    {"id": 10,"ema_short": 5, "ema_long": 15, "fractal_lookback": 3, "threshold": 0.3},
]

# ================== DATA STORAGE ==================
price_history = deque(maxlen=200)  # store latest ticks for EMA calculations
open_trades = []

# ================== UTILITY FUNCTIONS ==================
def calculate_ema(prices, length):
    """Simple EMA calculation"""
    if len(prices) < length:
        return None
    ema = sum(prices[-length:]) / length
    return ema

def detect_fractal(prices, lookback, threshold):
    """Detect fractal breakout"""
    if len(prices) < lookback * 2 + 1:
        return None
    center = prices[-lookback-1]
    left = prices[-lookback*2-1:-lookback-1]
    right = prices[-lookback:]
    if center > max(left + right) + threshold:
        return "up"
    elif center < min(left + right) - threshold:
        return "down"
    return None

def log_trade(setup_id, direction, price, stake):
    """Log trade details"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] Trade | Setup {setup_id} | {direction} | Price={price} | Stake={stake}")

# ================== TRADING LOGIC ==================
def check_setups():
    """Check all 10 Ultra Elite setups for signals"""
    for setup in ULTRA_ELITE_SETUPS:
        ema_short = calculate_ema(price_history, setup["ema_short"])
        ema_long = calculate_ema(price_history, setup["ema_long"])
        if ema_short is None or ema_long is None:
            continue
        fractal_signal = detect_fractal(price_history, setup["fractal_lookback"], setup["threshold"])
        if fractal_signal:
            # EMA alignment filter
            if fractal_signal == "up" and ema_short > ema_long:
                execute_trade(setup["id"], "buy")
            elif fractal_signal == "down" and ema_short < ema_long:
                execute_trade(setup["id"], "sell")

def execute_trade(setup_id, direction):
    """Send trade order if within risk limits"""
    if len(open_trades) >= MAX_OPEN_TRADES:
        return
    # Simplified order logic
    trade = {"setup_id": setup_id, "direction": direction, "stake": STAKE_PER_TRADE, "price": price_history[-1]}
    open_trades.append(trade)
    log_trade(setup_id, direction, price_history[-1], STAKE_PER_TRADE)
    # TODO: Replace with actual WebSocket order call
    # ws.send(json.dumps({...}))

# ================== WEBSOCKET MANAGEMENT ==================
def on_message(ws, message):
    data = json.loads(message)
    if "tick" in data:
        price = data["tick"]["quote"]
        price_history.append(price)
        check_setups()

def on_error(ws, error):
    print("WebSocket Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed. Reconnecting in 5s...")
    time.sleep(5)
    start_websocket()

def on_open(ws):
    print("WebSocket connected. Subscribing to USD/JPY ticks...")
    subscribe_msg = {
        "ticks": SYMBOL,
        "subscribe": 1
    }
    ws.send(json.dumps(subscribe_msg))

def start_websocket():
    ws = websocket.WebSocketApp(
        f"wss://ws.binaryws.com/websockets/v3?app_id={APP_ID}&l=EN",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

# ================== MAIN ==================
if __name__ == "__main__":
    print("Starting Ultra Elite USD/JPY Cloud Bot...")
    start_websocket()