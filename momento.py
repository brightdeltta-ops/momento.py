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

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOLATILITY_WINDOW = 15
VOLATILITY_THRESHOLD = 0.0005

PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)
trade_log = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
TRADE_AMOUNT = BASE_STAKE
WINS = LOSSES = TRADE_COUNT = 0

trade_in_progress = False
last_proposal_time = 0
last_direction = None

lock = threading.Lock()
ws = None
console = Console()

# ================= EQUITY SYSTEMS =================
EQUITY_LOCK = 0.0
EQUITY_STEP = 50.0
LOCK_TOLERANCE = 0.03

SESSION_START_BALANCE = 0.0
SESSION_LOCK = 0.0
SESSION_LOCK_PCT = 0.02

PROFIT_VAULT = 0.0
VAULT_STEP = 100.0

PAUSE_TRADING = False
HARD_LOCK = False

# ================= LEARNER =================
class OnlineLearner:
    def __init__(self, n):
        self.w = np.zeros(n)
        self.b = 0.0
        self.lr = 0.05
        self.decay = 0.999

    def margin(self, x):
        return np.dot(self.w, x) + self.b

    def predict(self, x):
        return 1 if self.margin(x) > 0 else -1

    def confidence(self, x):
        return min(1.0, abs(self.margin(x)) / (np.linalg.norm(self.w) + 1e-6))

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        err = y - self.predict(x)
        self.w *= self.decay
        conf = self.confidence(x)
        self.w += self.lr * err * conf * x
        self.b += self.lr * err * conf

learner = OnlineLearner(4)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1, 0, period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def extract_features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    a = np.array(tick_buffer)
    ef = calculate_ema(a, EMA_FAST)
    es = calculate_ema(a, EMA_SLOW)
    if ef is None or es is None:
        return None
    return np.array([
        ef - es,
        a[-1] - a.mean(),
        a.std(),
        (a[-1] - a[0]) / (a.std() + 1e-6)
    ])

def tradable_market():
    if len(tick_history) < VOLATILITY_WINDOW:
        return False
    r = np.array(list(tick_history)[-VOLATILITY_WINDOW:])
    return r.std() > VOLATILITY_THRESHOLD and abs(r[-1] - r[0]) > r.std()

def regime_strength():
    r = np.array(list(tick_history)[-VOLATILITY_WINDOW:])
    return abs(r[-1] - r[0]) / (r.std() + 1e-6)

# ================= EQUITY LOGIC =================
def update_equity_lock():
    global EQUITY_LOCK
    if BALANCE > EQUITY_LOCK + EQUITY_STEP:
        EQUITY_LOCK = BALANCE

def update_session_lock():
    global SESSION_LOCK
    if BALANCE > SESSION_LOCK * (1 + SESSION_LOCK_PCT):
        SESSION_LOCK = BALANCE

def update_profit_vault():
    global PROFIT_VAULT
    if BALANCE - PROFIT_VAULT >= VAULT_STEP:
        PROFIT_VAULT += VAULT_STEP

def equity_lock_check():
    global PAUSE_TRADING, HARD_LOCK
    if EQUITY_LOCK == 0:
        return True
    dd = (EQUITY_LOCK - BALANCE) / EQUITY_LOCK
    tol = LOCK_TOLERANCE if regime_strength() > 1.2 else LOCK_TOLERANCE / 2
    if dd > tol * 2:
        HARD_LOCK = True
        return False
    if dd > tol:
        PAUSE_TRADING = True
        return False
    return True

def risk_multiplier():
    if EQUITY_LOCK == 0:
        return 1.0
    dd = max(0, (EQUITY_LOCK - BALANCE) / EQUITY_LOCK)
    return max(0.2, 1 - dd * 3)

def calculate_dynamic_stake(conf):
    tradable = max(0, BALANCE - PROFIT_VAULT)
    return min(BASE_STAKE + conf * tradable * TRADE_RISK_FRAC, MAX_STAKE)

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    with lock:
        if trade_in_progress or HARD_LOCK or PAUSE_TRADING:
            return
        if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
            return
        if not tradable_market() or not equity_lock_check():
            return

        f = extract_features()
        if f is None:
            return

        direction = "up" if learner.predict(f) == 1 else "down"
        conf = learner.confidence(f)
        if conf < 0.25:
            return

        stake = calculate_dynamic_stake(conf) * risk_multiplier()
        stake = min(stake, MAX_STAKE)

        trade_queue.append((direction, 1, stake))
        last_proposal_time = time.time()
        last_direction = direction
        process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, s = trade_queue.popleft()
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
        trade_in_progress = True

def on_contract_settlement(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)

    WINS += profit > 0
    LOSSES += profit <= 0
    TRADE_COUNT += 1
    trade_in_progress = False

    f = extract_features()
    if f is not None:
        learner.update(f, profit)

    if profit > 0:
        update_equity_lock()
        update_session_lock()
        update_profit_vault()

# ================= WEBSOCKET =================
def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    def on_open(w):
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, m):
        global BALANCE, SESSION_START_BALANCE, SESSION_LOCK
        d = json.loads(m)

        if "authorize" in d:
            w.send(json.dumps({"balance": 1, "subscribe": 1}))
            w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
            SESSION_START_BALANCE = BALANCE
            SESSION_LOCK = BALANCE

        if "balance" in d:
            BALANCE = float(d["balance"]["balance"])

        if "tick" in d:
            t = float(d["tick"]["quote"])
            tick_history.append(t)
            tick_buffer.append(t)
            evaluate_and_trade()

        if "proposal" in d:
            time.sleep(PROPOSAL_DELAY)
            w.send(json.dumps({"buy": d["proposal"]["id"], "price": TRADE_AMOUNT}))

        if "proposal_open_contract" in d:
            c = d["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1):
        while True:
            table = Table(title="🚀 MOMENTO BOT — CAPITAL AWARE")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Equity Lock", f"{EQUITY_LOCK:.2f}")
            table.add_row("Profit Vault", f"{PROFIT_VAULT:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins / Losses", f"{WINS} / {LOSSES}")
            console.print(table)
            time.sleep(2)

# ================= START =================
if __name__ == "__main__":
    console.print("🚀 MOMENTO BOT STARTED — NEXT GEN")
    start_ws()
    threading.Thread(target=dashboard, daemon=True).start()
    while True:
        time.sleep(10)