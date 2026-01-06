import json, threading, time, websocket, numpy as np, os
from collections import deque
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from datetime import datetime

# ================= CONFIG (AGGRESSIVE MODE) =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

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
trade_queue = deque(maxlen=50)
trade_log = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE
trade_in_progress = False
last_proposal_time = 0
last_direction = None
stop_bot = False

ws = None
lock = threading.Lock()
console = Console()

# ================= META MEMORY =================
RECENT_RESULTS = deque(maxlen=50)

def performance_score():
    if len(RECENT_RESULTS) < 10:
        return 0.0
    return sum(RECENT_RESULTS) / len(RECENT_RESULTS)

def recent_failure_bias():
    if len(RECENT_RESULTS) < 5:
        return 1.0
    return 1.5 if sum(RECENT_RESULTS[-5:]) < 0 else 1.0

# ================= ONLINE LEARNER =================
class OnlineLearner:
    def __init__(self, n_features):
        self.weights = np.zeros(n_features)
        self.bias = 0.0
        self.lr = 0.1
        self.min_lr = 0.01
        self.max_lr = 0.3

    def predict(self, x):
        return 1 if np.dot(self.weights, x) + self.bias > 0 else -1

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        pred = self.predict(x)
        error = y - pred

        perf = performance_score()
        if perf < -0.2:
            self.lr = min(self.lr * 1.1, self.max_lr)
        elif perf > 0.2:
            self.lr = max(self.lr * 0.95, self.min_lr)

        bias = recent_failure_bias()
        self.weights += self.lr * error * x * bias
        self.bias += self.lr * error * bias

learner = OnlineLearner(n_features=4)

# ================= LOGGING =================
def log_tick(tick):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[cyan][{ts}] TICK {tick:.4f}[/cyan]")

def log_trade(direction, stake, profit):
    ts = datetime.now().strftime("%H:%M:%S")
    color = "green" if profit > 0 else "red" if profit < 0 else "yellow"
    console.log(f"[{color}][{ts}] Trade {direction} | Stake={stake:.2f} | P/L={profit:.2f}[/{color}]")

def log_proposal(direction, stake):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[magenta][{ts}] Proposal {direction} | Stake={stake:.2f}[/magenta]")

def log_heartbeat():
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[blue][{ts}] ❤️ HEARTBEAT[/blue]")

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def session_loss_check():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def prediction_confidence(features):
    raw = np.dot(learner.weights, features) + learner.bias
    return min(1.0, max(0.1, abs(raw)))

def calculate_dynamic_stake(confidence):
    perf = performance_score()
    risk_mult = 0.5 if perf < -0.3 else 1.3 if perf > 0.3 else 1.0
    return min(BASE_STAKE + confidence * BALANCE * TRADE_RISK_FRAC * risk_mult, MAX_STAKE)

def extract_features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    arr = np.array(tick_buffer)
    return np.array([
        calculate_ema(arr, EMA_FAST),
        calculate_ema(arr, EMA_SLOW),
        arr[-1] - arr[0],
        arr.std()
    ])

def record_trade_log(direction, stake, confidence, profit):
    trade_log.appendleft({
        "Direction": direction,
        "Stake": f"{stake:.2f}",
        "Confidence": f"{confidence:.2f}",
        "Profit": f"{profit:.2f}"
    })

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction
    if stop_bot or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check() or len(tick_history) < VOLATILITY_WINDOW:
        return
    if np.array(list(tick_history)[-VOLATILITY_WINDOW:]).std() < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None:
        return

    direction = "up" if learner.predict(features) == 1 else "down"
    confidence = prediction_confidence(features)
    TRADE_AMOUNT = calculate_dynamic_stake(confidence)

    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    last_direction = direction
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, stake = trade_queue.popleft()
        send_proposal(d, dur, stake)

def send_proposal(direction, duration, stake):
    global trade_in_progress
    ct = "CALL" if direction == "up" else "PUT"
    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": duration,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    log_proposal(ct, stake)
    trade_in_progress = True

def on_contract_settlement(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress, MAX_BALANCE
    profit = float(c.get("profit") or 0)

    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    WINS += profit > 0
    LOSSES += profit <= 0
    TRADE_COUNT += 1
    trade_in_progress = False

    RECENT_RESULTS.append(1 if profit > 0 else -1)

    record_trade_log(last_direction or "N/A", TRADE_AMOUNT, 0.0, profit)
    log_trade(last_direction or "N/A", TRADE_AMOUNT, profit)

    features = extract_features()
    if features is not None:
        learner.update(features, profit)

# ================= WEBSOCKET =================
def resubscribe():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def start_ws():
    global ws
    DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    def on_open(w):
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(ws, msg):
        data = json.loads(msg)
        if "authorize" in data and not data["authorize"].get("error"):
            resubscribe()
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            log_tick(tick)
            evaluate_and_trade()
        if "proposal" in data:
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy": data["proposal"]["id"], "price": TRADE_AMOUNT}))
        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
        if "balance" in data:
            global BALANCE
            BALANCE = float(data["balance"]["balance"])

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard_loop():
    with Live(refresh_per_second=1) as live:
        while True:
            table = Table(title="🚀 Momento Bot — Self-Aware AGI-Lite")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))
            table.add_row("Performance", f"{performance_score():.2f}")
            live.update(table)
            log_heartbeat()
            time.sleep(5)

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 MOMENTO BOT STARTED — SELF-AWARE MODE[/green]")
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        time.sleep(10)