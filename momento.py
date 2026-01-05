import json, threading, time, websocket, os, pandas as pd, numpy as np
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
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

SYMBOL = "R_75"
BASE_STAKE = 1.0
MAX_STAKE = 200.0
TRADE_RISK_FRAC = 0.05
MAX_CONSECUTIVE_LOSSES = 3

PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOLATILITY_WINDOW = 15
VOLATILITY_THRESHOLD = 0.0005
MAX_DD = 0.25

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=50)
trade_log = deque(maxlen=20)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE
trade_in_progress = False
consecutive_losses = 0
last_proposal_time = 0
last_direction = None
stop_bot = False

ws = None
lock = threading.Lock()
console = Console()

# ================= ONLINE LEARNER =================
class OnlineLearner:
    def __init__(self, n_features):
        self.weights = np.zeros(n_features)
        self.bias = 0.0
        self.lr = 0.1

    def predict(self, x):
        x = np.array(x) / (np.std(x) + 1e-6)  # normalize
        return 1 if np.dot(self.weights, x) + self.bias > 0 else -1

    def update(self, x, profit):
        x = np.array(x) / (np.std(x) + 1e-6)
        y = 1 if profit > 0 else -1
        error = y - self.predict(x)
        self.weights += self.lr * error * x
        self.bias += self.lr * error

learner = OnlineLearner(n_features=4)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    series = pd.Series(data[-period:])
    return series.ewm(span=period, adjust=False).mean().iloc[-1]

def session_loss_check():
    with lock:
        return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def calculate_dynamic_stake(confidence):
    global consecutive_losses
    stake = min(BASE_STAKE + confidence * BALANCE * TRADE_RISK_FRAC, MAX_STAKE)
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        stake = min(stake, BASE_STAKE)
    return stake

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
    with lock:
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
    confidence = 0.7
    TRADE_AMOUNT = calculate_dynamic_stake(confidence)

    with lock:
        trade_queue.append((direction, 1, TRADE_AMOUNT))
        last_proposal_time = time.time()
        last_direction = direction

    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    with lock:
        if trade_queue and not trade_in_progress:
            direction, duration, stake = trade_queue.popleft()
            send_proposal(direction, duration, stake)

def send_proposal(direction, duration, stake):
    global trade_in_progress
    ct = "CALL" if direction == "up" else "PUT"
    try:
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
        console.log(f"[magenta]📤 Proposal sent {ct} | Stake=${stake:.2f}[/magenta]")
        trade_in_progress = True
    except Exception as e:
        console.log(f"[red]Send proposal error: {e}[/red]")
        trade_in_progress = False

def on_contract_settlement(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress, MAX_BALANCE, consecutive_losses
    profit = float(c.get("profit") or 0)
    with lock:
        BALANCE += profit
        MAX_BALANCE = max(MAX_BALANCE, BALANCE)
        WINS += profit > 0
        LOSSES += profit <= 0
        TRADE_COUNT += 1
        trade_in_progress = False
        consecutive_losses = consecutive_losses + 1 if profit <= 0 else 0
        record_trade_log(last_direction or "N/A", TRADE_AMOUNT, 0.7, profit)
    log_trade(last_direction or "N/A", TRADE_AMOUNT, profit)
    features = extract_features()
    if features is not None:
        learner.update(features, profit)

# ================= WEBSOCKET (Original Style) =================
def resubscribe():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
    console.log("[green]Subscribed to ticks, balance, contracts[/green]")

def start_ws():
    global ws
    DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    console.log(f"[yellow]Connecting to Deriv WebSocket at {DERIV_WS}[/yellow]")

    def on_open(w):
        console.log("[green]WebSocket connected[/green]")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            if "authorize" in data:
                if data["authorize"].get("error"):
                    console.log(f"[red]Auth failed: {data['authorize']['error']}[/red]")
                else:
                    console.log("[green]✅ Authorized[/green]")
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
                with lock:
                    BALANCE = float(data["balance"]["balance"])
        except Exception as e:
            console.log(f"[red]on_message error: {e}[/red]")

    def on_error(ws, error):
        console.log(f"[red]WebSocket ERROR: {error}[/red]")

    def on_close(ws, code, msg):
        console.log(f"[red]WebSocket closed | Code: {code} | Msg: {msg}[/red]")

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= DASHBOARD =================
# (Same as before)

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 Momento Bot starting on Koyeb [AGGRESSIVE MODE] with improved safety[/green]")
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        time.sleep(5)