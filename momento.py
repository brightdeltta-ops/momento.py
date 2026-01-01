import json, threading, time, websocket, numpy as np
from collections import deque
import sys, os
from rich.console import Console
from rich.table import Table
from rich.live import Live
from http.server import BaseHTTPRequestHandler, HTTPServer

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # from Render env vars
APP_ID = int(os.getenv("APP_ID"))         # from Render env vars
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
last_trade_time = 0
last_proposal_time = 0
last_direction = None
stop_bot = False
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
        pred = self.predict(x)
        error = y - pred
        self.weights += self.lr * error * x
        self.bias += self.lr * error

learner = OnlineLearner(n_features=4)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period: return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def session_loss_check():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def calculate_dynamic_stake(confidence):
    return min(BASE_STAKE + confidence * BALANCE * TRADE_RISK_FRAC, MAX_STAKE)

def check_trade_memory(direction):
    global last_direction
    if direction == last_direction: return False
    last_direction = direction
    return True

def extract_features():
    if len(tick_buffer) < MICRO_SLICE: return None
    arr = np.array(tick_buffer)
    short_ema = calculate_ema(arr, EMA_FAST)
    long_ema = calculate_ema(arr, EMA_SLOW)
    slope = arr[-1]-arr[0]
    vol = arr.std()
    return np.array([short_ema, long_ema, slope, vol])

# ================= TRADE STRATEGY =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if stop_bot: return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN: return
    if not session_loss_check(): return
    if len(tick_history) < VOLATILITY_WINDOW or np.array(list(tick_history)[-VOLATILITY_WINDOW:]).std() < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None: return

    direction = "up" if learner.predict(features) == 1 else "down"
    confidence = 0.7

    short_ema, long_ema = features[0], features[1]
    ema_trend = "up" if short_ema > long_ema else "down"
    if direction != ema_trend: return
    if not check_trade_memory(direction): return

    TRADE_AMOUNT = calculate_dynamic_stake(confidence)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    recent_signals.append(direction)
    last_proposal_time = time.time()
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if stop_bot: return
    if trade_queue and not trade_in_progress:
        direction,duration,stake = trade_queue.popleft()
        send_proposal(direction,duration,stake)

def send_proposal(direction,duration,stake):
    global trade_in_progress,last_trade_time
    if stop_bot: return
    ct = "CALL" if direction=="up" else "PUT"
    ws.send(json.dumps({
        "proposal":1,
        "amount":stake,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":duration,
        "duration_unit":"t",
        "symbol":SYMBOL
    }))
    trade_in_progress=True
    last_trade_time=time.time()

def on_contract_settlement(c):
    global BALANCE,WINS,LOSSES,TRADE_COUNT,trade_in_progress,MAX_BALANCE
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    if profit > 0: WINS+=1
    else: LOSSES+=1
    TRADE_COUNT+=1
    trade_in_progress=False
    features = extract_features()
    if features is not None:
        learner.update(features, profit)
    
# ================= WEBSOCKET =================
def on_message(ws,msg):
    global BALANCE
    try:
        data=json.loads(msg)
        if "authorize" in data:
            resubscribe_channels()
        if "tick" in data:
            tick=float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            evaluate_and_trade()
        if "proposal" in data:
            pid=data["proposal"]["id"]
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy":pid,"price":TRADE_AMOUNT}))
        if "proposal_open_contract" in data:
            c=data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
        if "balance" in data:
            BALANCE=float(data["balance"]["balance"])
    except: pass

def resubscribe_channels():
    ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
    ws.send(json.dumps({"balance":1,"subscribe":1}))
    ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))

def start_ws():
    global ws
    ws=websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=lambda ws: ws.send(json.dumps({"authorize":API_TOKEN})),
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever,daemon=True).start()

# ================= DASHBOARD =================
def render_dashboard():
    table = Table(title="Momento Bot Dashboard", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Balance", f"{BALANCE:.2f} USD")
    table.add_row("Wins", str(WINS))
    table.add_row("Losses", str(LOSSES))
    table.add_row("Trades", str(TRADE_COUNT))
    table.add_row("Open Trades", str(len(trade_queue)+int(trade_in_progress)))
    table.add_row("Stake", f"{TRADE_AMOUNT:.2f} USD")
    table.add_row("Last Signals", " ".join(recent_signals))
    return table

def dashboard_loop():
    with Live(render_dashboard(), refresh_per_second=2) as live:
        while not stop_bot:
            time.sleep(0.5)
            live.update(render_dashboard())

# ================= KEEP BOT ALIVE =================
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain")
        self.end_headers()
        self.wfile.write(b"Momento Bot is alive!")

def run_keep_alive_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    console.print(f"[bold green]🟢 Keep-alive server running on port {port}[/bold green]")
    server.serve_forever()

threading.Thread(target=run_keep_alive_server, daemon=True).start()

# ================= START BOT =================
def start():
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while not stop_bot:
        time.sleep(1)

if __name__=="__main__":
    console.print("[bold green]🚀 Momento Bot started[/bold green]")
    start()