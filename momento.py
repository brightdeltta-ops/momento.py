import json
import threading
import time
import websocket
import os
import numpy as np
from collections import deque
from datetime import datetime

# ============================================================
# KOYEB ENV CONFIG
# ============================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))
SYMBOL = os.getenv("SYMBOL", "R_75")

if not API_TOKEN or APP_ID == 0:
    raise RuntimeError("❌ Missing DERIV_API_TOKEN or APP_ID")

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ============================================================
# STRATEGY CONFIG
# ============================================================
TRADE_AMOUNT = 10.0
EMA_FAST, EMA_SLOW, EMA_SIGNAL = 6, 12, 25
AUTO_LOT_STEP = 0.5
MAX_LOSS_MARTI = 5
TREND_STRENGTH_THRESHOLD = 0.0

# ============================================================
# STATE
# ============================================================
tick_history = deque(maxlen=250)

balance = 0.0
wins = 0
losses = 0
trade_count = 0

trade_in_progress = False
martingale_level = 0
current_trade_amount = TRADE_AMOUNT
consecutive_wins = 0

ema_fast_val = 0.0
ema_slow_val = 0.0
ema_signal_val = 0.0

ws = None

# ============================================================
# LOGGING
# ============================================================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ============================================================
# EMA & TREND
# ============================================================
def calc_ema(prev, price, period):
    alpha = 2.0 / (period + 1)
    if prev == 0:
        return price
    return prev * (1 - alpha) + price * alpha

def detect_trend():
    if abs(ema_fast_val - ema_slow_val) < 1e-6:
        return None
    if ema_fast_val > ema_slow_val > ema_signal_val:
        return "RISE"
    if ema_fast_val < ema_slow_val < ema_signal_val:
        return "FALL"
    return None

def detect_trend_strength():
    if len(tick_history) < 50:
        return 0.0
    arr = np.array(list(tick_history)[-50:])
    std = np.std(arr)
    if std == 0:
        return 0.0
    return min(abs(arr[-1] - arr[0]) / std, 1.0)

# ============================================================
# TRADE EXECUTION
# ============================================================
def fire_trade(direction):
    global trade_in_progress

    if trade_in_progress:
        return

    payload = {
        "proposal": 1,
        "amount": current_trade_amount,
        "basis": "stake",
        "contract_type": "CALL" if direction == "RISE" else "PUT",
        "currency": "USD",
        "duration": 2,
        "duration_unit": "t",
        "symbol": SYMBOL
    }

    ws.send(json.dumps(payload))
    trade_in_progress = True
    log(f"🔥 Proposal sent → {direction} | stake={current_trade_amount:.2f}")

# ============================================================
# ENGINE LOOP
# ============================================================
def engine():
    global ema_fast_val, ema_slow_val, ema_signal_val

    while True:
        if len(tick_history) < EMA_SIGNAL:
            time.sleep(0.05)
            continue

        price = tick_history[-1]

        ema_fast_val = calc_ema(ema_fast_val, price, EMA_FAST)
        ema_slow_val = calc_ema(ema_slow_val, price, EMA_SLOW)
        ema_signal_val = calc_ema(ema_signal_val, price, EMA_SIGNAL)

        trend = detect_trend()
        strength = detect_trend_strength()

        if trend and not trade_in_progress and strength >= TREND_STRENGTH_THRESHOLD:
            fire_trade(trend)

        time.sleep(0.05)

# ============================================================
# WEBSOCKET HANDLERS
# ============================================================
def on_open(ws_conn):
    log("Connected → Authorizing")
    ws_conn.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws_conn, msg):
    global balance, wins, losses, trade_count
    global martingale_level, current_trade_amount
    global trade_in_progress, consecutive_wins

    data = json.loads(msg)

    if "authorize" in data:
        ws_conn.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws_conn.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws_conn.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✔ Authorized & Subscribed")

    if "tick" in data:
        tick_history.append(float(data["tick"]["quote"]))

    if "balance" in data:
        balance = float(data["balance"]["balance"])

    if "proposal" in data:
        ws_conn.send(json.dumps({"buy": data["proposal"]["id"], "price": current_trade_amount}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit", 0))
            trade_count += 1

            if profit > 0:
                wins += 1
                consecutive_wins += 1
                martingale_level = 0
                current_trade_amount = TRADE_AMOUNT + consecutive_wins * AUTO_LOT_STEP
                log(f"✔ WIN {profit:.2f}")
            else:
                losses += 1
                consecutive_wins = 0
                martingale_level += 1
                if martingale_level <= MAX_LOSS_MARTI:
                    current_trade_amount = TRADE_AMOUNT * (2 ** martingale_level)
                else:
                    martingale_level = 0
                    current_trade_amount = TRADE_AMOUNT
                log(f"✖ LOSS {profit:.2f}")

            balance += profit
            trade_in_progress = False

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat():
    while True:
        time.sleep(30)
        log(f"❤️ HEARTBEAT | BAL={balance:.2f} W={wins} L={losses}")

# ============================================================
# START
# ============================================================
def start():
    global ws

    log("🚀 EMA TREND BOT — KOYEB READY")

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=lambda _, e: log(f"❌ WS error {e}"),
        on_close=lambda *_: log("❌ WS closed")
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()
    threading.Thread(target=engine, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

    while True:
        time.sleep(10)

if __name__ == "__main__":
    start()