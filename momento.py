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
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

SYMBOL = "R_75"
BASE_STAKE = 1.0
MAX_STAKE = 200.0
TRADE_RISK_FRAC = 0.05  # Aggressive fraction
PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOLATILITY_WINDOW = 15
VOLATILITY_THRESHOLD = 0.0005  # Aggressive
MAX_DD = 0.25  # Max drawdown allowed

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
MODE = "AGGRESSIVE"

# Drawdown thresholds
DRAW_THRESHOLD_AGGRESSIVE = 0.20
DRAW_THRESHOLD_SAFE = 0.10

EXPECTED_GROWTH = {7: "140-180", 30: "400-800", 60: "1500-3000", 90: "5000+"}

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
        return 1 if np.dot(self.weights, x) + self.bias > 0 else -1

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        error = y - self.predict(x)
        self.weights += self.lr * error * x
        self.bias += self.lr * error

learner = OnlineLearner(n_features=4)

# ================= LOGGING =================
def log_tick(tick):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[cyan][{ts}] TICK {tick:.4f}[/cyan]")

def log_trade(direction, stake, profit, trade_id=None):
    ts = datetime.now().strftime("%H:%M:%S")
    trade_label = f"[ID:{trade_id}]" if trade_id else ""
    if profit > 0:
        console.log(f"[green]{trade_label}[{ts}] ✅ {MODE} Trade {direction.upper()} | Stake=${stake:.2f} | Profit=${profit:.2f}[/green]")
    elif profit < 0:
        console.log(f"[red]{trade_label}[{ts}] ❌ {MODE} Trade {direction.upper()} | Stake=${stake:.2f} | Loss=${profit:.2f}[/red]")
    else:
        console.log(f"[yellow]{trade_label}[{ts}] ⚪ {MODE} Trade {direction.upper()} | Stake=${stake:.2f} | Break-even[/yellow]")

def log_proposal(direction, stake):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[magenta][{ts}] 📤 Proposal sent {direction.upper()} | Stake=${stake:.2f} | Mode={MODE}[/magenta]")

def log_heartbeat():
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[blue][{ts}] ❤️ HEARTBEAT: {MODE} mode, bot running, no new trades[/blue]")

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

def adjust_mode():
    global MODE, VOLATILITY_THRESHOLD, TRADE_RISK_FRAC, MAX_DD
    if MAX_BALANCE == 0:
        return
    drawdown = (MAX_BALANCE - BALANCE) / MAX_BALANCE
    if MODE == "AGGRESSIVE" and drawdown > DRAW_THRESHOLD_AGGRESSIVE:
        MODE = "SAFE"
        VOLATILITY_THRESHOLD = 0.001
        TRADE_RISK_FRAC = 0.02
        MAX_DD = 0.1
        console.log(f"[yellow]⚠️ Drawdown {drawdown:.2%} → Switching to SAFE mode[/yellow]")
    elif MODE == "SAFE" and drawdown < DRAW_THRESHOLD_SAFE:
        MODE = "AGGRESSIVE"
        VOLATILITY_THRESHOLD = 0.0005
        TRADE_RISK_FRAC = 0.05
        MAX_DD = 0.25
        console.log(f"[green]✅ Recovery {drawdown:.2%} → Switching to AGGRESSIVE mode[/green]")

def calculate_dynamic_cac():
    confidence = 0.7
    return calculate_dynamic_stake(confidence)

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction
    if stop_bot or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check() or len(tick_history) < VOLATILITY_WINDOW:
        return
    adjust_mode()
    if np.array(list(tick_history)[-VOLATILITY_WINDOW:]).std() < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None:
        return

    direction = "up" if learner.predict(features) == 1 else "down"
    TRADE_AMOUNT = calculate_dynamic_cac()
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    last_direction = direction
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
    record_trade_log(last_direction or "N/A", TRADE_AMOUNT, 0.7, profit)
    log_trade(last_direction or "N/A", TRADE_AMOUNT, profit, TRADE_COUNT)

    features = extract_features()
    if features is not None:
        learner.update(features, profit)

# ================= DASHBOARD =================
def dashboard_loop():
    with Live(auto_refresh=True, refresh_per_second=1) as live:
        last_trade_count = -1
        while True:
            table = Table(title=f"🚀 Momento Bot Dashboard [MODE: {MODE}]")
            table.add_column("Metric", justify="left")
            table.add_column("Value", justify="right")

            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))

            # Expected growth
            for days, growth in EXPECTED_GROWTH.items():
                table.add_row(f"Expected {days}d Growth ($100 start)", growth)

            # Heartbeat
            if TRADE_COUNT == last_trade_count:
                log_heartbeat()
                table.add_row("❤️ HEARTBEAT", datetime.now().strftime("%H:%M:%S") + " | No new trades")
            else:
                last_trade_count = TRADE_COUNT
                table.add_row("✅ Last Trade Update", f"Balance={BALANCE:.2f}")

            # Last trades table
            table.add_section()
            table.add_row("[bold]Last Trades[/bold]", "")
            trade_table = Table()
            trade_table.add_column("Dir")
            trade_table.add_column("Stake")
            trade_table.add_column("Conf")
            trade_table.add_column("Profit")

            for t in trade_log:
                profit = float(t["Profit"])
                profit_text = Text(f"{profit:.2f}")
                if profit > 0:
                    profit_text.stylize("green")
                elif profit < 0:
                    profit_text.stylize("red")
                trade_table.add_row(
                    t["Direction"],
                    t["Stake"],
                    t["Confidence"],
                    profit_text
                )

            table.add_row("", trade_table)
            live.update(table)
            time.sleep(1)

# ================= START =================
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
                    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
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

if __name__ == "__main__":
    console.print("[green]🚀 Momento Bot starting on Koyeb [SMART CAC & PROFESSIONAL LOGGING][/green]")
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        time.sleep(5)