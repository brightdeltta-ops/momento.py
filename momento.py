import json, threading, time, websocket, numpy as np
from collections import deque
import os
from rich.console import Console
from rich.table import Table
from rich.live import Live

# ================= CONFIG =================
API_TOKEN = os.getenv("API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing API_TOKEN or APP_ID environment variables")

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
recent_signals = deque(maxlen=10)

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

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

console = Console()

# ================= ONLINE LEARNER =================
class OnlineLearner:
    def __init__(self, n_features):
        self.weights = np.zeros(n_features)
        self.bias = 0.0
        self.lr = 0.1

    def predict(self, x):
        return 1 if np.dot(self.weights, x) + self.bias > 0 else -1

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        error = y - self.predict(x)
        self.weights += self.lr * error * x
        self.bias += self.lr * error

learner = OnlineLearner(n_features=4)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def session_loss_check():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def calculate_dynamic_stake(confidence):
    return min(BASE_STAKE + confidence * BALANCE * TRADE_RISK_FRAC, MAX_STAKE)

def check_trade_memory(direction):
    global last_direction
    if direction == last_direction:
        return False
    last_direction = direction
    return True

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

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if stop_bot:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        return

    if len(tick_history) < VOLATILITY_WINDOW:
        return
    if np.array(list(tick_history)[-VOLATILITY_WINDOW:]).std() < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None:
        return

    direction = "up" if learner.predict(features) == 1 else "down"
    ema_trend = "up" if features[0] > features[1] else "down"
    if direction != ema_trend:
        return
    if not check_trade_memory(direction):
        return

    confidence = 0.7
    TRADE_AMOUNT = calculate_dynamic_stake(confidence)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        direction, duration, stake = trade_queue.popleft()
        send_proposal(direction, duration, stake)

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
    features = extract_features()
    if features is not None:
        learner.update(features, profit)

# ================= WEBSOCKET =================
def on_message(ws, msg):
    try:
        data = json.loads(msg)
        if "authorize" in data:
            resubscribe()
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
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
    except Exception:
        pass

def resubscribe():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda w: w.send(json.dumps({"authorize": API_TOKEN})),
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
def dashboard_loop():
    with Live(auto_refresh=True) as live:
        while True:
            table = Table(title="Momento Bot")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))
            live.update(table)
            time.sleep(2)

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 Momento Bot starting on Koyeb[/green]")
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        time.sleep(5)