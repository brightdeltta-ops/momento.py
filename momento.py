import os, json, threading, time, websocket, numpy as np, pickle
from collections import deque
from rich.console import Console
from rich.table import Table
from rich.live import Live

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 200.0

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOL_WINDOW = 15
VOL_THRESHOLD = 0.00025

LOSS_STREAK_LIMIT = 3
MAX_DD = 0.10

PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 2

STATE_FILE = "/tmp/learner_state.pkl"

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
TRADE_COUNT = 0
LOSS_STREAK = 0
TRADE_AMOUNT = BASE_STAKE
trade_in_progress = False
last_direction = None
LAST_LOSS_DIRECTION = None
last_proposal_time = 0

ws = None
console = Console()

# ================= LEARNER =================
class OnlineLearner:
    def __init__(self, n):
        self.w = np.zeros(n)
        self.b = 0.0
        self.lr = 0.08

    def predict(self, x):
        z = np.dot(self.w, x) + self.b
        z = max(min(z, 100), -100)
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
        try:
            with open(STATE_FILE, "wb") as f:
                pickle.dump((self.w, self.b), f)
        except:
            pass

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "rb") as f:
                    self.w, self.b = pickle.load(f)
            except:
                pass

learner = OnlineLearner(4)
learner.load()

# ================= UTILS =================
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

# ================= REGIME CLASSIFIER =================
def classify_regime():
    if len(tick_history) < VOL_WINDOW:
        return "NONE"

    recent = np.array(list(tick_history)[-VOL_WINDOW:])
    vol = recent.std()
    fast = ema(recent, EMA_FAST)
    slow = ema(recent, EMA_SLOW)
    sep = abs(fast - slow)
    momentum = recent[-1] - recent[0]

    if vol < VOL_THRESHOLD or sep < vol * 0.1:
        return "CHOP"

    if vol > VOL_THRESHOLD * 1.8 and abs(momentum) > vol * 0.8:
        return "BREAKOUT"

    return "TREND"

# ================= RISK =================
def session_ok():
    if MAX_BALANCE == 0:
        return True
    dd = (MAX_BALANCE - BALANCE) / MAX_BALANCE
    return dd < MAX_DD

def dynamic_stake(conf, regime):
    core = BASE_STAKE
    bonus = conf * BASE_STAKE * (2.0 if regime == "BREAKOUT" else 1.0)
    stake = core + bonus

    if LOSS_STREAK >= LOSS_STREAK_LIMIT:
        stake *= 0.5

    return min(stake, MAX_STAKE)

# ================= TRADING =================
def evaluate():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_ok():
        return

    regime = classify_regime()
    if regime == "CHOP":
        return

    f = features()
    if f is None:
        return

    direction, conf = learner.predict(f)

    if TRADE_COUNT < 20:
        conf = min(conf, 0.4)

    conf *= max(0.3, 1 - LOSS_STREAK * 0.25)

    if LOSS_STREAK >= 2 and direction == LAST_LOSS_DIRECTION:
        return

    if conf < 0.1:
        return

    TRADE_AMOUNT = dynamic_stake(conf, regime)
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
    console.log(f"[magenta]{ct} | Stake {s:.2f}[/magenta]")
    trade_in_progress = True

def settle(c):
    global BALANCE, MAX_BALANCE, TRADE_COUNT, LOSS_STREAK
    global trade_in_progress, LAST_LOSS_DIRECTION

    profit = float(c.get("profit", 0))
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    TRADE_COUNT += 1
    LOSS_STREAK = LOSS_STREAK + 1 if profit <= 0 else 0
    LAST_LOSS_DIRECTION = last_direction if profit <= 0 else None

    trade_in_progress = False
    console.log(f"[green]P/L {profit:.2f} | Balance {BALANCE:.2f}[/green]")

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
            console.log(f"[red]WS error {e}[/red]")

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1) as live:
        while True:
            t = Table(title="🚀 MOMENTO BOT — REGIME ENGINE")
            t.add_column("Metric")
            t.add_column("Value")
            t.add_row("Balance", f"{BALANCE:.2f}")
            t.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            t.add_row("Trades", str(TRADE_COUNT))
            t.add_row("Loss Streak", str(LOSS_STREAK))
            t.add_row("Regime", classify_regime())
            live.update(t)
            time.sleep(1)

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 MOMENTO BOT STARTED — REGIME AWARE[/green]")
    start_ws()
    threading.Thread(target=dashboard, daemon=True).start()
    while True:
        time.sleep(10)