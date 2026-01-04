import os
import websocket
import json
import threading
import time
from collections import deque
import numpy as np

# ================= USER CONFIG =================
DERIV_TOKEN = os.getenv("DERIV_TOKEN")  # Must be set in Koyeb secrets
SYMBOL = "R_75"  # V75 Synthetic Index
BASE_STAKE = 1.0
MAX_STAKE = 50.0
MIN_TICKS = 15
PROFIT_TARGET = 5.0  # % per successful cycle
TICK_BUFFER = 50  # store last N ticks

# EMA Settings for 1s tick
FAST_EMA_PERIOD = 5
SLOW_EMA_PERIOD = 12

# ================================================

# Tick storage
ticks = deque(maxlen=TICK_BUFFER)

# EMA helper
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    prices_array = np.array(prices[-period:])
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.dot(prices_array, weights)

# Fractal breakout detection (simple)
def detect_fractal(prices):
    if len(prices) < 7:
        return None
    mid = len(prices) // 2
    if prices[mid] > max(prices[mid-3:mid]+prices[mid+1:mid+4]):
        return "up"
    elif prices[mid] < min(prices[mid-3:mid]+prices[mid+1:mid+4]):
        return "down"
    return None

# Trading logic
def generate_signal():
    if len(ticks) < MIN_TICKS:
        return None
    tick_list = list(ticks)
    ema_fast = calculate_ema(tick_list, FAST_EMA_PERIOD)
    ema_slow = calculate_ema(tick_list, SLOW_EMA_PERIOD)
    fractal_signal = detect_fractal(tick_list[-7:])  # last 7 ticks for fractal

    if ema_fast is None or ema_slow is None:
        return None

    if ema_fast > ema_slow and fractal_signal == "up":
        return "buy"
    elif ema_fast < ema_slow and fractal_signal == "down":
        return "sell"
    return None

# Trade execution simulation (replace with real API call)
def execute_trade(signal, stake):
    print(f"[TRADE] {signal.upper()} | Stake: ${stake}")
    # Simulate trade success
    win = np.random.choice([True, False], p=[0.55, 0.45])
    return win

# Koyeb-safe WebSocket connection
def on_message(ws, message):
    data = json.loads(message)
    tick = data.get("tick")
    if tick is not None:
        ticks.append(tick)
        signal = generate_signal()
        if signal:
            stake = min(BASE_STAKE, MAX_STAKE)
            win = execute_trade(signal, stake)
            if win:
                print(f"[WIN] Signal {signal} successful!")
                # Optional: fast compounding
                global BASE_STAKE
                BASE_STAKE = min(BASE_STAKE * 1.2, MAX_STAKE)
            else:
                print(f"[LOSS] Signal {signal} failed.")
                BASE_STAKE = 1.0  # reset to base on loss

def on_error(ws, error):
    print(f"[WS ERROR] {error}")

def on_close(ws):
    print("[WS CLOSED] Connection closed")

def on_open(ws):
    print("[WS OPEN] Connected to Deriv API")

# Connect to Deriv WebSocket
def start_ws():
    url = f"wss://ws.binaryws.com/websockets/v3?app_id=1089&l=EN"
    ws = websocket.WebSocketApp(url,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.on_open = on_open
    ws.run_forever()

# Run bot
if __name__ == "__main__":
    threading.Thread(target=start_ws).start()