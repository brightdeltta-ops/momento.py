# ================= UNBUFFERED STDOUT (CRITICAL FOR KOYEB) =================
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ================= IMPORTS =================
import json
import threading
import time
import os
import websocket
import numpy as np
from collections import deque

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = os.getenv("APP_ID")

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

APP_ID = int(APP_ID)

SYMBOL = "R_75"

# ================= STRATEGY CONFIG =================
BASE_STAKE = 1.0
MAX_STAKE = 50.0
TRADE_RISK_FRAC = 0.02

EMA_FAST = 3
EMA_SLOW = 10

MICRO_SLICE = 10
VOLATILITY_WINDOW = 20
VOLATILITY_THRESHOLD = 0.0015

PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 12
MAX_DD = 0.2

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=5)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADES = 0

TRADE_AMOUNT = BASE_STAKE
last_proposal_time = 0
trade_in_progress = False
last_direction = None

ws = None
lock = threading.Lock()

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= ONLINE LEARNER =================
class OnlineLearner:
    def __init__(self):
        self.w = np.zeros(4)
        self.b = 0.0
        self.lr = 0.1

    def predict(self, x):
        return 1 if np.dot(self.w, x) + self.b > 0 else -1

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        err = y - self.predict(x)
        self.w += self.lr * err * x
        self.b += self.lr * err

learner = OnlineLearner()

# ================= UTILS =================
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def session_ok():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def dynamic_stake(conf=0.7):
    return min(BASE_STAKE + BALANCE * TRADE_RISK_FRAC * conf, MAX_STAKE)

def features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    arr = np.array(tick_buffer)
    return np.array([
        ema(arr, EMA_FAST),
        ema(arr, EMA_SLOW),
        arr[-1] - arr[0],
        arr.std()
    ])

# ================= TRADING LOGIC =================
def evaluate():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    if trade_in_progress:
        return

    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return

    if not session_ok():
        log("🛑 Max drawdown reached")
        return

    if len(tick_history) < VOLATILITY_WINDOW:
        return

    if np.std(list(tick_history)[-VOLATILITY_WINDOW:]) < VOLATILITY_THRESHOLD:
        return

    f = features()
    if f is None or f[0] is None or f[1] is None:
        return

    direction = "up" if learner.predict(f) == 1 else "down"
    trend = "up" if f[0] > f[1] else "down"

    if direction != trend:
        return

    if direction == last_direction:
        return

    last_direction = direction
    TRADE_AMOUNT = dynamic_stake()

    trade_queue.append((direction, TRADE_AMOUNT))
    last_proposal_time = time.time()

    send_trade()

def send_trade():
    global trade_in_progress

    if not trade_queue:
        return

    direction, stake = trade_queue.popleft()
    ct = "CALL" if direction == "up" else "PUT"

    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

    trade_in_progress = True
    log(f"📤 Proposal sent {ct} ${stake:.2f}")

# ================= WEBSOCKET HANDLERS =================
def on_message(_, msg):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADES, trade_in_progress

    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✅ Authorized & subscribed")

    if "tick" in data:
        price = float(data["tick"]["quote"])
        tick_history.append(price)
        tick_buffer.append(price)
        evaluate()

    if "proposal" in data:
        time.sleep(PROPOSAL_DELAY)
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": TRADE_AMOUNT}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit", 0))
            BALANCE += profit
            MAX_BALANCE = max(MAX_BALANCE, BALANCE)
            TRADES += 1
            WINS += profit > 0
            LOSSES += profit <= 0
            trade_in_progress = False

            f = features()
            if f is not None:
                learner.update(f, profit)

            log(f"📊 Trade closed | PnL={profit:.2f} | Bal={BALANCE:.2f}")

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])
        MAX_BALANCE = max(MAX_BALANCE, BALANCE)

def on_open(_):
    ws.send(json.dumps({"authorize": API_TOKEN}))
    log("🔗 WebSocket connected")

def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message
    )
    ws.run_forever()

# ================= HEARTBEAT =================
def heartbeat():
    while True:
        log(f"❤️ HEARTBEAT | Bal={BALANCE:.2f} Trades={TRADES} W/L={WINS}/{LOSSES}")
        time.sleep(60)

# ================= MAIN =================
if __name__ == "__main__":
    log("🚀 Momento Bot starting on Koyeb")
    threading.Thread(target=heartbeat, daemon=True).start()
    start_ws()