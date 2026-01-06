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
BASE_STAKE = 1.0
MAX_LOSS_MARTI = 5
AUTO_LOT_STEP = 0.5
TREND_STRENGTH_THRESHOLD = 0.6

EMA_FAST = 4
EMA_MEDIUM = 8
EMA_SLOW = 16

FRACTAL_WINDOW = 5
MICRO_TICKS = 3

TICK_COOLDOWN_MIN = 1
TICK_COOLDOWN_MAX = 4

# ============================================================
# STATE
# ============================================================
tick_history = deque(maxlen=500)
trade_memory = deque(maxlen=300)

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADES = 0

ema_fast = 0.0
ema_medium = 0.0
ema_slow = 0.0

martingale_level = 0
trade_in_progress = False
current_trade_amount = BASE_STAKE
ticks_since_last_trade = 0

ws = None

# ============================================================
# LOGGING
# ============================================================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ============================================================
# MATH UTILITIES
# ============================================================
def ema_update(prev, price, period):
    alpha = 2.0 / (period + 1)
    if prev == 0:
        return price
    return prev * (1 - alpha) + price * alpha

def detect_trend():
    if ema_fast > ema_medium > ema_slow:
        return "RISE"
    if ema_fast < ema_medium < ema_slow:
        return "FALL"
    return None

def detect_fractal():
    if len(tick_history) < FRACTAL_WINDOW:
        return None
    arr = list(tick_history)[-FRACTAL_WINDOW:]
    mid = FRACTAL_WINDOW // 2
    if arr[mid] == max(arr):
        return "UP"
    if arr[mid] == min(arr):
        return "DOWN"
    return None

def detect_strength():
    if len(tick_history) < 20:
        return 0.0
    arr = np.array(list(tick_history)[-20:])
    return min(abs(arr[-1] - arr[0]) / (np.std(arr) + 1e-8), 1.0)

def compute_confidence():
    slope = abs(ema_fast - ema_medium)
    strength = detect_strength()
    return min((slope / 0.1) * 0.5 + strength * 0.5, 1.0)

def micro_predict(direction):
    if len(tick_history) < MICRO_TICKS + 1:
        return True
    arr = np.array(list(tick_history)[-MICRO_TICKS-1:])
    avg_delta = np.mean(np.diff(arr))
    return (direction == "RISE" and avg_delta > 0) or \
           (direction == "FALL" and avg_delta < 0)

# ============================================================
# ADAPTIVE LEARNING
# ============================================================
ADAPTIVE_MIN_CONF = 0.6

def dynamic_confidence(direction):
    relevant = [t for t in trade_memory if t["dir"] == direction]
    if not relevant:
        return 1.0
    wins = sum(1 for t in relevant if t["res"] == "WIN")
    return max(wins / len(relevant), ADAPTIVE_MIN_CONF)

# ============================================================
# TRADE EXECUTION
# ============================================================
def fire_trade(direction):
    global trade_in_progress

    if trade_in_progress:
        return

    base_conf = compute_confidence()
    dyn_conf = dynamic_confidence(direction)

    if base_conf < dyn_conf:
        log(f"⚠ Skip {direction} | base={base_conf:.2f} dyn={dyn_conf:.2f}")
        return

    trade_in_progress = True
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
    log(f"🔥 Proposal {direction} | stake={current_trade_amount:.2f}")

# ============================================================
# ENGINE LOOP
# ============================================================
def engine():
    global ema_fast, ema_medium, ema_slow
    global ticks_since_last_trade, trade_in_progress

    while True:
        if len(tick_history) < EMA_SLOW:
            time.sleep(0.05)
            continue

        price = tick_history[-1]

        ema_fast = ema_update(ema_fast, price, EMA_FAST)
        ema_medium = ema_update(ema_medium, price, EMA_MEDIUM)
        ema_slow = ema_update(ema_slow, price, EMA_SLOW)

        trend = detect_trend()
        strength = detect_strength()
        fractal = detect_fractal()
        confidence = compute_confidence()

        cooldown = int(
            TICK_COOLDOWN_MAX
            - strength * (TICK_COOLDOWN_MAX - TICK_COOLDOWN_MIN)
        )

        ticks_since_last_trade += 1

        if (
            trend
            and strength >= TREND_STRENGTH_THRESHOLD
            and ticks_since_last_trade >= cooldown
            and confidence >= 0.65
            and micro_predict(trend)
            and not (trend == "RISE" and fractal == "DOWN")
            and not (trend == "FALL" and fractal == "UP")
        ):
            fire_trade(trend)
            ticks_since_last_trade = 0

        if trade_in_progress and ticks_since_last_trade > 120:
            trade_in_progress = False
            log("⚠ Trade timeout reset")

        time.sleep(0.05)

# ============================================================
# WEBSOCKET HANDLER
# ============================================================
def on_message(_, msg):
    global BALANCE, WINS, LOSSES, TRADES
    global martingale_level, trade_in_progress, current_trade_amount

    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✔ Authorized & Subscribed")

    if "tick" in data:
        tick_history.append(float(data["tick"]["quote"]))

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

    if "proposal" in data:
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": current_trade_amount}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit", 0))
            result = "WIN" if profit > 0 else "LOSS"

            trade_memory.append({"dir": detect_trend(), "res": result})

            if profit > 0:
                WINS += 1
                martingale_level = 0
                current_trade_amount = BASE_STAKE + WINS * AUTO_LOT_STEP
            else:
                LOSSES += 1
                martingale_level += 1
                if martingale_level <= MAX_LOSS_MARTI:
                    current_trade_amount = BASE_STAKE * (2 ** martingale_level)
                else:
                    martingale_level = 0
                    current_trade_amount = BASE_STAKE

            BALANCE += profit
            TRADES += 1
            trade_in_progress = False

            log(f"✔ Settled {profit:.2f} | BAL={BALANCE:.2f} W={WINS} L={LOSSES}")

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat():
    while True:
        time.sleep(30)
        log("❤️ HEARTBEAT")

# ============================================================
# START
# ============================================================
def start():
    global ws
    log("🚀 FRACTAL EMA BOT — KOYEB READY")

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda w: w.send(json.dumps({"authorize": API_TOKEN})),
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