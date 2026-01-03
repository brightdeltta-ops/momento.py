import json
import threading
import time
import websocket
import numpy as np
from collections import deque
import os
from rich.console import Console
from rich.table import Table
from rich.live import Live

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN") or os.getenv("API_TOKEN")
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

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_proposal_time = 0
last_direction = None

ws = None
console = Console()

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

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

# ================= UTILITIES =================
def ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1, 0, period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def session_loss_check():
    if MAX_BALANCE == 0:
        return True
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def dynamic_stake(conf):
    return min(BASE_STAKE + conf * BALANCE * TRADE_RISK_FRAC, MAX_STAKE)

def extract_features():
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
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        return
    if len(tick_history) < VOLATILITY_WINDOW:
        return
    if np.std(list(tick_history)[-VOLATILITY_WINDOW:]) < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None or None in features:
        return

    direction = "up" if learner.predict(features) == 1 else "down"
    ema_trend = "up" if features[0] > features[1] else "down"

    if direction != ema_trend:
        return
    if direction == last_direction:
        return

    last_direction = direction
    TRADE_AMOUNT = dynamic_stake(0.7)

    console.log(
        f"[yellow]SIGNAL[/yellow] {direction.upper()} "
        f"Stake={TRADE_AMOUNT:.2f}"
    )

    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    if trade_queue and not trade_in_progress:
        d, t, s = trade_queue.popleft()
        send_proposal(d, t, s)

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
    console.log(f"[blue]PROPOSAL SENT[/blue] {ct} {stake:.2f}")

def settle_trade(contract):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress

    profit = float(contract.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)

    if profit > 0:
        WINS += 1
    else:
        LOSSES += 1

    TRADE_COUNT += 1
    trade_in_progress = False

    console.log(
        f"[green]RESULT[/green] {'WIN' if profit > 0 else 'LOSS'} "
        f"Profit={profit:.2f} Balance={BALANCE:.2f}"
    )

    f = extract_features()
    if f is not None:
        learner.update(f, profit)

# ================= WEBSOCKET =================
def on_message(_, msg):
    try:
        data = json.loads(msg)

        if "authorize" in data:
            subscribe()

        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            console.log(f"[cyan]TICK[/cyan] {tick}")
            evaluate_and_trade()

        if "proposal" in data:
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({
                "buy": data["proposal"]["id"],
                "price": TRADE_AMOUNT
            }))

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                settle_trade(c)

        if "balance" in data:
            global BALANCE
            BALANCE = float(data["balance"]["balance"])

    except Exception as e:
        console.log(f"[red]ERROR[/red] {e}")

def subscribe():
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
    ws.run_forever()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1):
        while True:
            table = Table(title="Momento Cloud Bot")
            table.add_column("Metric")
            table.add_column("Value")
            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))
            time.sleep(2)

# ================= START =================
if __name__ == "__main__":
    console.print("[bold green]🚀 Momento Bot LIVE on Koyeb[/bold green]")
    threading.Thread(target=dashboard, daemon=True).start()
    start_ws()