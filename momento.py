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
MAX_STAKE = 100.0
TRADE_RISK_FRAC = 0.02

PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 12

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOLATILITY_WINDOW = 20
VOLATILITY_THRESHOLD = 0.0015
MAX_DD = 0.2

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)
trade_log = deque(maxlen=5)

BALANCE = 0.0
MAX_BALANCE = 0.0
TRADE_AMOUNT = BASE_STAKE

WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_proposal_time = 0
last_direction = None
stop_bot = False

ws = None
console = Console()

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
        self.w += self.lr * err * x
        self.b += self.lr * err

learner = OnlineLearner(4)

# ================= LOGGING =================
def ts():
    return datetime.now().strftime("%H:%M:%S")

def log_tick(t):
    console.log(f"[cyan][{ts()}] TICK {t:.4f}[/cyan]")

def log_skip(reason):
    console.log(f"[yellow][{ts()}] ⏭ SKIP: {reason}[/yellow]")

def log_proposal(d, s):
    console.log(f"[magenta][{ts()}] 📤 PROPOSAL {d.upper()} | ${s:.2f}[/magenta]")

def log_trade(d, s, p):
    if p > 0:
        console.log(f"[green][{ts()}] ✅ WIN {d} | ${s:.2f} | +${p:.2f}[/green]")
    elif p < 0:
        console.log(f"[red][{ts()}] ❌ LOSS {d} | ${s:.2f} | ${p:.2f}[/red]")
    else:
        console.log(f"[yellow][{ts()}] ⚪ BE {d} | ${s:.2f}[/yellow]")

def log_heartbeat():
    console.log(f"[blue][{ts()}] ❤️ HEARTBEAT[/blue]")

# ================= UTILITIES =================
def ema(arr, p):
    if len(arr) < p:
        return None
    w = np.exp(np.linspace(-1., 0., p))
    w /= w.sum()
    return np.convolve(arr[-p:], w, mode="valid")[0]

def session_ok():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def stake(conf):
    return min(BASE_STAKE + BALANCE * conf * TRADE_RISK_FRAC, MAX_STAKE)

def features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    a = np.array(tick_buffer)
    return np.array([ema(a, EMA_FAST), ema(a, EMA_SLOW), a[-1]-a[0], a.std()])

def trade_memory(d):
    global last_direction
    if d == last_direction:
        return False
    last_direction = d
    return True

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT

    if stop_bot:
        return

    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        log_skip("Cooldown")
        return

    if not session_ok():
        log_skip("Drawdown limit")
        return

    if len(tick_history) < VOLATILITY_WINDOW:
        log_skip("Not enough ticks")
        return

    if np.std(list(tick_history)[-VOLATILITY_WINDOW:]) < VOLATILITY_THRESHOLD:
        log_skip("Low volatility")
        return

    f = features()
    if f is None or None in f:
        log_skip("Features incomplete")
        return

    direction = "up" if learner.predict(f) == 1 else "down"
    trend = "up" if f[0] > f[1] else "down"

    if direction != trend:
        log_skip("EMA mismatch")
        return

    if not trade_memory(direction):
        log_skip("Repeat direction")
        return

    TRADE_AMOUNT = stake(0.7)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_trade_queue()

def process_trade_queue():
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
    log_proposal(d, s)
    trade_in_progress = True

def settle(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    p = float(c.get("profit") or 0)
    BALANCE += p
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)

    WINS += p > 0
    LOSSES += p <= 0
    TRADE_COUNT += 1
    trade_in_progress = False

    log_trade(last_direction or "N/A", TRADE_AMOUNT, p)

    f = features()
    if f is not None:
        learner.update(f, p)

# ================= WEBSOCKET =================
def resub():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    def on_open(w):
        console.log("[green]WS Connected[/green]")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_msg(w, m):
        data = json.loads(m)

        if "authorize" in data and not data["authorize"].get("error"):
            console.log("[green]Authorized[/green]")
            resub()

        if "tick" in data:
            t = float(data["tick"]["quote"])
            tick_history.append(t)
            tick_buffer.append(t)
            log_tick(t)
            evaluate_and_trade()

        if "proposal" in data:
            time.sleep(PROPOSAL_DELAY)
            w.send(json.dumps({"buy": data["proposal"]["id"], "price": TRADE_AMOUNT}))

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                settle(c)

        if "balance" in data:
            global BALANCE
            BALANCE = float(data["balance"]["balance"])

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_msg)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1):
        while True:
            log_heartbeat()
            time.sleep(5)

# ================= START =================
if __name__ == "__main__":
    console.print("[bold green]🚀 Momento Bot LIVE on Koyeb[/bold green]")
    start_ws()
    threading.Thread(target=dashboard, daemon=True).start()
    while True:
        time.sleep(10)