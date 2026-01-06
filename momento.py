import json, threading, time, websocket, numpy as np, os
from collections import deque
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))
if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

SYMBOL = "R_75"
BASE_STAKE = 1.0
MAX_STAKE = 200.0
TRADE_RISK_FRAC = 0.05

PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOLATILITY_WINDOW = 15
VOLATILITY_THRESHOLD = 0.0005
MAX_DD = 0.15

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)
trade_log = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = LOSSES = TRADE_COUNT = 0

TRADE_AMOUNT = BASE_STAKE
trade_in_progress = False
last_proposal_time = 0
last_direction = None

ACTIVE_CONTRACT_ID = None
SETTLEMENT_LOCK = False

ws = None
console = Console()

# ================= META MEMORY =================
RECENT_RESULTS = deque(maxlen=50)

def performance_score():
    if len(RECENT_RESULTS) < 10:
        return 0.0
    return sum(RECENT_RESULTS) / len(RECENT_RESULTS)

def recent_failure_bias():
    return 1.5 if len(RECENT_RESULTS) >= 5 and sum(RECENT_RESULTS[-5:]) < 0 else 1.0

# ================= ONLINE LEARNER =================
class OnlineLearner:
    def __init__(self, n):
        self.w = np.zeros(n)
        self.b = 0.0
        self.lr = 0.1

    def predict(self, x):
        return 1 if np.dot(self.w, x) + self.b > 0 else -1

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        err = y - self.predict(x)
        self.lr = min(0.3, max(0.01, self.lr * (1.1 if profit < 0 else 0.95)))
        bias = recent_failure_bias()
        self.w += self.lr * err * x * bias
        self.b += self.lr * err * bias

learner = OnlineLearner(4)

# ================= UTILITIES =================
def ema(arr, p):
    if len(arr) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(arr[-p:], w, mode="valid")[0]

def extract_features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    a = np.array(tick_buffer)
    return np.array([ema(a, EMA_FAST), ema(a, EMA_SLOW), a[-1]-a[0], a.std()])

def prediction_confidence(x):
    return min(1.0, max(0.1, abs(np.dot(learner.w, x) + learner.b)))

def dynamic_stake(conf):
    perf = performance_score()
    mult = 0.5 if perf < -0.3 else 1.3 if perf > 0.3 else 1.0
    return min(BASE_STAKE + conf * BALANCE * TRADE_RISK_FRAC * mult, MAX_STAKE)

def loss_guard():
    return (MAX_BALANCE - BALANCE) < MAX_BALANCE * MAX_DD if MAX_BALANCE > 0 else True

# ================= LOGGING =================
def log(msg, color="white"):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[{color}][{ts}] {msg}[/{color}]")

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction
    if trade_in_progress or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not loss_guard() or len(tick_history) < VOLATILITY_WINDOW:
        return
    if np.std(list(tick_history)[-VOLATILITY_WINDOW:]) < VOLATILITY_THRESHOLD:
        return

    feats = extract_features()
    if feats is None:
        return

    last_direction = "up" if learner.predict(feats) == 1 else "down"
    conf = prediction_confidence(feats)
    TRADE_AMOUNT = dynamic_stake(conf)

    trade_queue.append((last_direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, amt = trade_queue.popleft()
        send_proposal(d, dur, amt)

def send_proposal(d, dur, amt):
    global trade_in_progress
    ct = "CALL" if d == "up" else "PUT"
    ws.send(json.dumps({
        "proposal": 1,
        "amount": amt,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": dur,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    trade_in_progress = True
    log(f"Proposal {ct} | Stake={amt:.2f}", "magenta")

def on_settlement(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress, SETTLEMENT_LOCK
    if SETTLEMENT_LOCK or last_direction is None:
        return
    SETTLEMENT_LOCK = True

    profit = float(c.get("profit", 0))
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    WINS += profit > 0
    LOSSES += profit <= 0
    TRADE_COUNT += 1
    trade_in_progress = False

    RECENT_RESULTS.append(1 if profit > 0 else -1)

    feats = extract_features()
    if feats is not None:
        learner.update(feats, profit)

    log(f"Trade {last_direction} | Stake={TRADE_AMOUNT:.2f} | P/L={profit:.2f}",
        "green" if profit > 0 else "red")

    SETTLEMENT_LOCK = False

# ================= WEBSOCKET =================
def start_ws():
    global ws, ACTIVE_CONTRACT_ID
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    def on_open(w):
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(ws, msg):
        global ACTIVE_CONTRACT_ID, BALANCE
        d = json.loads(msg)

        if "authorize" in d and not d["authorize"].get("error"):
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

        if "tick" in d:
            t = float(d["tick"]["quote"])
            tick_history.append(t)
            tick_buffer.append(t)
            evaluate_and_trade()

        if "proposal" in d:
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy": d["proposal"]["id"], "price": TRADE_AMOUNT}))

        if "proposal_open_contract" in d:
            c = d["proposal_open_contract"]

            if c.get("is_open") and ACTIVE_CONTRACT_ID is None:
                ACTIVE_CONTRACT_ID = c.get("contract_id")

            if c.get("contract_id") != ACTIVE_CONTRACT_ID:
                return

            if c.get("is_sold") or c.get("is_expired"):
                on_settlement(c)
                ACTIVE_CONTRACT_ID = None

        if "balance" in d:
            BALANCE = float(d["balance"]["balance"])

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1) as live:
        while True:
            t = Table(title="🚀 MOMENTO BOT — CORRECTED")
            t.add_column("Metric")
            t.add_column("Value")
            t.add_row("Balance", f"{BALANCE:.2f}")
            t.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            t.add_row("Trades", str(TRADE_COUNT))
            t.add_row("Wins", str(WINS))
            t.add_row("Losses", str(LOSSES))
            t.add_row("Performance", f"{performance_score():.2f}")
            live.update(t)
            time.sleep(5)

# ================= START =================
if __name__ == "__main__":
    log("🚀 MOMENTO BOT STARTED — CORRECTED MODE", "green")
    start_ws()
    threading.Thread(target=dashboard, daemon=True).start()
    while True:
        time.sleep(10)