# ================= MOMENTO MICRO FRACTAL BOT — KOYEB READY =================
import json, threading, time, websocket, os, numpy as np
from collections import deque
from datetime import datetime

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = "R_75"

# ================= STRATEGY CONFIG =================
BASE_STAKE = 1.0
MAX_STAKE = 100.0
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 2
TRADE_TIMEOUT = 12

PROFIT_LOCK = 50.0        # lock profits after +50
MAX_SESSION_LOSS = -50.0  # hard stop

# ================= STATE =================
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)

BALANCE = 0.0
SESSION_PEAK = 0.0
WINS = 0
LOSSES = 0
EDGE = 0.0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
authorized = False

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOG =================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ================= EMA =================
def ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

# ================= SIGNAL =================
def breakout_signal():
    if len(tick_buffer) < 5:
        return None, 0
    arr = np.array(tick_buffer)
    high, low = arr.max(), arr.min()
    rng = high - low
    if rng < 0.002:
        return None, 0
    last = arr[-1]
    std = arr.std()

    if last >= high - 0.2 * rng:
        return "up", min(1, std * 200)
    if last <= low + 0.2 * rng:
        return "down", min(1, std * 200)
    return None, 0

# ================= TRADE LOGIC =================
def evaluate_trade():
    global last_proposal_time

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if BALANCE <= MAX_SESSION_LOSS:
        log("🛑 MAX SESSION LOSS HIT — HALTING")
        return

    direction, confidence = breakout_signal()
    if confidence < 0.5:
        return

    fast = ema(list(tick_buffer), EMA_FAST)
    slow = ema(list(tick_buffer), EMA_SLOW)
    if fast is None or slow is None:
        return

    trend = "up" if fast > slow else "down"
    if direction != trend:
        return

    stake = min(MAX_STAKE, BASE_STAKE * (1 + confidence))
    trade_queue.append((direction, stake))
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    if trade_queue and not trade_in_progress:
        direction, stake = trade_queue.popleft()
        send_proposal(direction, stake)

def send_proposal(direction, stake):
    global trade_in_progress, last_trade_time
    ct = "CALL" if direction == "up" else "PUT"
    payload = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }
    trade_in_progress = True
    last_trade_time = time.time()
    ws.send(json.dumps(payload))
    log(f"📨 PROPOSAL {ct} | stake={stake}")

# ================= WS EVENTS =================
def on_message(ws, msg):
    global BALANCE, WINS, LOSSES, trade_in_progress, SESSION_PEAK, EDGE
    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✅ AUTHORIZED")

    if "tick" in data:
        tick = float(data["tick"]["quote"])
        tick_buffer.append(tick)
        evaluate_trade()

    if "proposal" in data:
        pid = data["proposal"]["id"]
        time.sleep(PROPOSAL_DELAY)
        ws.send(json.dumps({"buy": pid, "price": data["proposal"]["ask_price"]}))
        log("🔥 BUY SENT")

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit", 0))
            BALANCE += profit
            SESSION_PEAK = max(SESSION_PEAK, BALANCE)
            EDGE = BALANCE - SESSION_PEAK
            WINS += profit > 0
            LOSSES += profit <= 0
            trade_in_progress = False
            log(f"✔ SETTLED | P/L={profit:.2f} BAL={BALANCE:.2f}")

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

def on_open(ws):
    ws.send(json.dumps({"authorize": API_TOKEN}))
    log("🌐 CONNECTING")

def on_close(ws, *_):
    log("❌ WS CLOSED — RECONNECTING")
    time.sleep(2)
    start_ws()

def on_error(ws, err):
    log(f"⚠ WS ERROR {err}")

# ================= SAFETY =================
def watchdog():
    global trade_in_progress
    while True:
        time.sleep(3)
        if trade_in_progress and time.time() - last_trade_time > TRADE_TIMEOUT:
            trade_in_progress = False
            log("⚠ TRADE TIMEOUT RESET")

def heartbeat():
    while True:
        time.sleep(30)
        log(f"❤️ BAL={BALANCE:.2f} W={WINS} L={LOSSES} EDGE={EDGE:.2f}")

# ================= START =================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

if __name__ == "__main__":
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    start_ws()