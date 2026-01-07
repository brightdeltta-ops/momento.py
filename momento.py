# ==========================================
# MOMENTO — FIRING BOT + PROFIT LOCK (KOYEB)
# ==========================================

import os, json, time, threading, websocket
from collections import deque
from datetime import datetime, timezone
import numpy as np

# ============== ENV ==================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))
SYMBOL = os.getenv("SYMBOL", "R_75")
BASE_STAKE = float(os.getenv("BASE_STAKE", "1"))

# ============== PARAMETERS ===========
EMA_FAST = 5
EMA_SLOW = 21
WINDOW = 30

CONF_THRESHOLD = 0.55
COOLDOWN = 2.0
MAX_EDGE = 0.25
TRADE_TIMEOUT = 8

# ---- PROFIT LOCK ----
LOCK_TRIGGER_PCT = 0.02     # 2%
LOCK_RATIO = 0.50           # lock 50%

# ============== STATE ================
ticks = deque(maxlen=WINDOW)
balance = 0.0
start_balance = 0.0
session_peak = 0.0
locked_balance = 0.0

wins = 0
losses = 0
edge = 0.0

last_trade_ts = 0.0
trade_in_progress = False
trade_sent_ts = 0.0

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

# ============== PROFIT LOCK ==========
def update_profit_lock():
    global session_peak, locked_balance
    session_peak = max(session_peak, balance)
    gain = session_peak - start_balance
    if start_balance > 0 and gain / start_balance >= LOCK_TRIGGER_PCT:
        locked_balance = start_balance + gain * LOCK_RATIO

def profit_lock_ok():
    if locked_balance > 0 and balance <= locked_balance:
        log("🔒 PROFIT LOCK HIT — TRADING HALTED")
        return False
    return True

# ============== SIGNAL =================
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

# ============== TRADING =================
def maybe_trade():
    global last_trade_ts, trade_in_progress, trade_sent_ts

    if trade_in_progress:
        if time.time() - trade_sent_ts > TRADE_TIMEOUT:
            log("⚠ TRADE TIMEOUT — RESET")
            trade_in_progress = False
        return

    if not profit_lock_ok():
        return

    now = time.time()
    if now - last_trade_ts < COOLDOWN:
        return

    direction, conf = detect_signal()
    if not direction or conf < CONF_THRESHOLD:
        return

    stake = round(BASE_STAKE * (1 + edge), 2)
    if locked_balance > 0:
        stake *= 0.7  # reduce risk after lock

    send_trade(direction, stake)
    last_trade_ts = now
    trade_in_progress = True
    trade_sent_ts = time.time()

def send_trade(direction, stake):
    log(f"📨 PROPOSAL {direction} | stake={stake}")
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
    log("🔥 BUY SENT")

# ============== WS HANDLERS ===========
def on_message(wsapp, message):
    global balance, wins, losses, edge, trade_in_progress, start_balance

    data = json.loads(message)

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        maybe_trade()

    elif "balance" in data:
        balance = float(data["balance"]["balance"])
        if start_balance == 0:
            start_balance = balance

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0))
            trade_in_progress = False

            if profit > 0:
                wins += 1
                edge = min(MAX_EDGE, edge + 0.03)
            else:
                losses += 1
                edge = max(0.0, edge - 0.02)

            update_profit_lock()

def on_open(wsapp):
    log("CONNECTED & AUTHORIZED")
    wsapp.send(json.dumps({"authorize": API_TOKEN}))
    wsapp.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    wsapp.send(json.dumps({"balance": 1, "subscribe": 1}))
    wsapp.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def on_error(wsapp, error):
    log(f"WS ERROR: {error}")

def on_close(wsapp, code, msg):
    log("WS CLOSED — RECONNECTING")
    time.sleep(2)
    start_ws()

# ============== HEARTBEAT ============
def heartbeat():
    while True:
        log(f"❤️ BAL={balance:.2f} W={wins} L={losses} EDGE={edge:.2f} LOCK={locked_balance:.2f}")
        time.sleep(30)

# ============== MAIN ==================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)

if __name__ == "__main__":
    log("🚀 MOMENTO — FIRING BOT + PROFIT LOCK (KOYEB READY)")
    threading.Thread(target=heartbeat, daemon=True).start()
    start_ws()