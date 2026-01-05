import json, threading, time, websocket, numpy as np, os, pickle
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
RISK_FRAC = 0.05

PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 5

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOL_WINDOW = 15
VOL_THRESHOLD = 0.00025
MAX_DD = 0.25
LOSS_STREAK_LIMIT = 3

STATE_FILE = "learner_state.pkl"

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=20)
trade_log = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
LOSS_STREAK = 0

TRADE_AMOUNT = BASE_STAKE
last_proposal_time = 0
trade_in_progress = False
last_direction = None

ws = None
console = Console()

# ================= LOGGING =================
def log_tick(t):
    console.log(f"[cyan]TICK {t:.4f}[/cyan]")

def log_trade(d, s, p):
    c = "green" if p > 0 else "red"
    console.log(f"[{c}]Trade {d} | Stake {s:.2f} | P/L {p:.2f}[/{c}]")

def log_proposal(d, s):
    console.log(f"[magenta]Proposal {d} | Stake {s:.2f}[/magenta]")

def log_heartbeat():
    console.log("[blue]❤️ HEARTBEAT[/blue]")

# ================= LEARNER =================
class OnlineLearner:
    def __init__(self, n):
        self.w = np.zeros(n)
        self.b = 0.0
        self.lr = 0.1

    def predict(self, x):
        z = np.dot(self.w, x) + self.b
        p = 1 / (1 + np.exp(-z))
        conf = abs(p - 0.5) * 2
        return ("up" if z > 0 else "down"), conf

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        z = np.dot(self.w, x) + self.b
        y_hat = 1 if z > 0 else -1
        err = y - y_hat
        self.w += self.lr * err * x
        self.b += self.lr * err

    def save(self):
        with open(STATE_FILE, "wb") as f:
            pickle.dump((self.w, self.b), f)

    def load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "rb") as f:
                self.w, self.b = pickle.load(f)

learner = OnlineLearner(4)
learner.load()

# ================= UTIL =================
def ema(data, p):
    if len(data) < p:
        return 0.0
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(data[-p:], w, mode="valid")[0]

def features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    a = np.array(tick_buffer)
    return np.array([
        ema(a, EMA_FAST),
        ema(a, EMA_SLOW),
        a[-1] - a[0],
        max(a.std(), 1e-6)
    ])

def dynamic_stake(conf):
    stake = BASE_STAKE + conf * BALANCE * RISK_FRAC
    if LOSS_STREAK >= LOSS_STREAK_LIMIT:
        stake *= 0.5
    return min(max(stake, BASE_STAKE), MAX_STAKE)

def session_ok():
    return MAX_BALANCE == 0 or (MAX_BALANCE - BALANCE) < MAX_BALANCE * MAX_DD

# ================= TRADING =================
def evaluate():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_ok():
        return
    if len(tick_history) < VOL_WINDOW:
        return
    if np.std(list(tick_history)[-VOL_WINDOW:]) < VOL_THRESHOLD:
        return

    f = features()
    if f is None:
        return

    direction, conf = learner.predict(f)

    if TRADE_COUNT < 10:
        conf = 0.7
    if conf < 0.1:
        return

    TRADE_AMOUNT = dynamic_stake(conf)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_direction = direction
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, s = trade_queue.popleft()
        send_proposal(d, dur, s)

def send_proposal(d, dur, s):
    global trade_in_progress
    ct = "CALL" if d == "up" else "PUT"
    ws.send(json.dumps({
        "proposal": 1,
        "amount": s,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": dur,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    log_proposal(ct, s)
    trade_in_progress = True

def settle(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, LOSS_STREAK, trade_in_progress

    profit = float(c.get("profit", 0))
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    TRADE_COUNT += 1
    LOSS_STREAK = LOSS_STREAK + 1 if profit <= 0 else 0
    WINS += profit > 0
    LOSSES += profit <= 0

    trade_in_progress = False
    log_trade(last_direction, TRADE_AMOUNT, profit)

    f = features()
    if f is not None:
        learner.update(f, profit)
        learner.save()

# ================= WS =================
def resub():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    console.log(f"Connecting {url}")

    def on_open(w):
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        try:
            d = json.loads(msg)

            if "authorize" in d and not d["authorize"].get("error"):
                resub()

            if "tick" in d:
                t = float(d["tick"]["quote"])
                tick_history.append(t)
                tick_buffer.append(t)
                log_tick(t)
                evaluate()

            if "proposal" in d:
                time.sleep(PROPOSAL_DELAY)
                w.send(json.dumps({"buy": d["proposal"]["id"], "price": TRADE_AMOUNT}))

            if "proposal_open_contract" in d:
                c = d["proposal_open_contract"]
                if c.get("is_sold") or c.get("is_expired"):
                    settle(c)

            if "balance" in d:
                global BALANCE
                BALANCE = float(d["balance"]["balance"])

        except Exception as e:
            console.log(f"[red]on_message error {e}[/red]")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1) as live:
        last = -1
        while True:
            t = Table(title="🚀 MOMENTO BOT — INTELLIGENT PROFIT MODE")
            t.add_column("Metric")
            t.add_column("Value")
            t.add_row("Balance", f"{BALANCE:.2f}")
            t.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            t.add_row("Trades", str(TRADE_COUNT))
            t.add_row("Wins", str(WINS))
            t.add_row("Losses", str(LOSSES))
            t.add_row("Loss Streak", str(LOSS_STREAK))

            if TRADE_COUNT == last:
                log_heartbeat()
            last = TRADE_COUNT

            live.update(t)
            time.sleep(1)

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 MOMENTO BOT STARTED — STABLE & PROFITABLE[/green]")
    start_ws()
    threading.Thread(target=dashboard, daemon=True).start()
    while True:
        time.sleep(5)