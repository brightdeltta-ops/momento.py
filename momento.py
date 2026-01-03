import json
import threading
import time
import websocket
import numpy as np
import os
from collections import deque
from rich.console import Console

# ================= CONSOLE =================
console = Console()

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = os.getenv("APP_ID")

if not API_TOKEN or not APP_ID:
    console.log("[red]❌ Missing DERIV_API_TOKEN or APP_ID[/red]")
    # Do NOT crash hard in cloud – wait and retry
    while True:
        time.sleep(60)

APP_ID = int(APP_ID)

# ================= STRATEGY CONFIG =================
SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
RISK_FRAC = 0.02

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOL_WINDOW = 20
VOL_THRESHOLD = 0.0015

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

trade_in_progress = False
last_trade_time = 0
last_direction = None

ws = None
lock = threading.Lock()

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

# ================= UTILS =================
def ema(arr, p):
    if len(arr) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(arr[-p:], w, mode="valid")[0]

def session_ok():
    if MAX_BALANCE == 0:
        return True
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def dynamic_stake(conf=0.7):
    return min(BASE_STAKE + BALANCE * RISK_FRAC * conf, MAX_STAKE)

def features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    arr = np.array(tick_buffer)
    return np.array([
        ema(arr, EMA_FAST) or 0,
        ema(arr, EMA_SLOW) or 0,
        arr[-1] - arr[0],
        arr.std()
    ])

# ================= TRADING LOGIC =================
def evaluate():
    global last_trade_time

    if trade_in_progress:
        return
    if time.time() - last_trade_time < PROPOSAL_COOLDOWN:
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

    direction = "up" if learner.predict(f) == 1 else "down"
    trend = "up" if f[0] > f[1] else "down"
    if direction != trend:
        return

    stake = dynamic_stake()
    trade_queue.append((direction, stake))
    last_trade_time = time.time()
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
    console.log(f"[yellow]📤 Proposal sent {ct} ${stake:.2f}[/yellow]")

# ================= WS CALLBACKS =================
def on_message(_, msg):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADES, trade_in_progress

    try:
        data = json.loads(msg)

        if "authorize" in data:
            console.log("[green]✅ Authorized[/green]")
            subscribe()

        if "tick" in data:
            price = float(data["tick"]["quote"])
            tick_history.append(price)
            tick_buffer.append(price)
            evaluate()

        if "proposal" in data:
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy": data["proposal"]["id"], "price": data["proposal"]["ask_price"]}))

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
                learner.update(features() or np.zeros(4), profit)

                console.log(
                    f"[bold cyan]📊 Trade closed | Profit={profit:.2f} | "
                    f"Bal={BALANCE:.2f} | W/L={WINS}/{LOSSES}[/bold cyan]"
                )

        if "balance" in data:
            BALANCE = float(data["balance"]["balance"])
            MAX_BALANCE = max(MAX_BALANCE, BALANCE)

    except Exception as e:
        console.log(f"[red]⚠ WS Error: {e}[/red]")

def subscribe():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

# ================= HEARTBEAT =================
def heartbeat():
    while True:
        console.log(
            f"[blue]❤️ HEARTBEAT | Bal={BALANCE:.2f} "
            f"Trades={TRADES} W/L={WINS}/{LOSSES}[/blue]"
        )
        time.sleep(60)

# ================= START =================
def start():
    global ws
    console.print("[green]🚀 Momento Bot starting on Koyeb[/green]")

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda w: w.send(json.dumps({"authorize": API_TOKEN})),
        on_message=on_message
    )

    threading.Thread(target=heartbeat, daemon=True).start()
    ws.run_forever(ping_interval=30, ping_timeout=10)

if __name__ == "__main__":
    start()