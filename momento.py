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
MAX_DD = 0.25

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
console = Console()

# ================= ALPHA LEARNER =================
class OnlineLearner:
    def __init__(self, n_features):
        self.weights = np.zeros(n_features)
        self.bias = 0.0
        self.lr = 0.1
        self.signal_history = deque(maxlen=50)

    def predict(self, x):
        raw = np.dot(self.weights, x) + self.bias
        confidence = 1 / (1 + np.exp(-raw))  # sigmoid 0-1
        direction = 1 if raw > 0 else -1
        return direction, confidence

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        pred, _ = self.predict(x)
        error = y - pred
        self.weights += self.lr * error * x
        self.bias += self.lr * error
        self.signal_history.append((pred, profit))

    def confidence_adjusted(self):
        if not self.signal_history:
            return 0.5
        wins = sum(1 for d, p in self.signal_history if d == 1 and p > 0)
        total = len(self.signal_history)
        return max(0.2, min(0.9, wins / total))

learner = OnlineLearner(n_features=7)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def session_loss_check():
    if MAX_BALANCE == 0:
        return True
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def extract_features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    arr = np.array(tick_buffer)
    ema_fast = calculate_ema(arr, EMA_FAST)
    ema_slow = calculate_ema(arr, EMA_SLOW)
    if ema_fast is None or ema_slow is None:
        return None
    features = np.array([
        ema_fast,
        ema_slow,
        arr[-1] - arr[0],
        arr.std(),
        arr[-1] - np.mean(arr),
        arr[-1] - arr[-2],
        ema_fast - ema_slow
    ])
    return features

def calculate_dynamic_stake(confidence_adjusted):
    stake = BASE_STAKE + confidence_adjusted * BALANCE * TRADE_RISK_FRAC
    return min(stake, MAX_STAKE)

def record_trade_log(direction, stake, confidence, profit):
    trade_log.appendleft({
        "Direction": direction,
        "Stake": f"{stake:.2f}",
        "Confidence": f"{confidence:.2f}",
        "Profit": f"{profit:.2f}"
    })

# ================= LOGGING =================
def log_tick(tick):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[bold cyan][{ts}] TICK {tick:.4f}[/bold cyan]")

def log_trade(direction, stake, profit):
    ts = datetime.now().strftime("%H:%M:%S")
    if profit > 0:
        console.log(f"[green][{ts}] ✅ Trade {direction} | Stake=${stake:.2f} | Profit=${profit:.2f}[/green]")
    elif profit < 0:
        console.log(f"[red][{ts}] ❌ Trade {direction} | Stake=${stake:.2f} | Loss=${profit:.2f}[/red]")
    else:
        console.log(f"[yellow][{ts}] ⚪ Trade {direction} | Stake=${stake:.2f} | Break-even[/yellow]")

def log_proposal(direction, stake):
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[magenta][{ts}] 📤 Proposal sent {direction.upper()} | Stake=${stake:.2f}[/magenta]")

def log_heartbeat():
    ts = datetime.now().strftime("%H:%M:%S")
    console.log(f"[blue][{ts}] ❤️ HEARTBEAT: Bot running, no new trades[/blue]")

# ================= ALPHA DASHBOARD =================
def dashboard_alpha_score():
    if not learner.signal_history:
        return 0.0
    alpha_score = sum((p if d==1 else -p) for d,p in learner.signal_history) / len(learner.signal_history)
    return alpha_score

def dashboard_loop():
    with Live(auto_refresh=True, refresh_per_second=1) as live:
        last_trade_count = -1
        while True:
            table = Table(title="🚀 Momento Bot Dashboard [ALPHA MODE]")
            table.add_column("Metric", justify="left")
            table.add_column("Value", justify="right")

            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))
            table.add_row("Alpha Score", f"{dashboard_alpha_score():.2f}")

            if TRADE_COUNT == last_trade_count:
                log_heartbeat()
                table.add_row("❤️ HEARTBEAT", datetime.now().strftime("%H:%M:%S") + " | No new trades")
            else:
                last_trade_count = TRADE_COUNT
                table.add_row("✅ Last Trade Update", f"Balance={BALANCE:.2f}")

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

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction

    if stop_bot or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check() or len(tick_history) < VOLATILITY_WINDOW:
        return

    recent_std = np.array(list(tick_history)[-VOLATILITY_WINDOW:]).std()
    if recent_std < VOLATILITY_THRESHOLD:
        return

    features = extract_features()
    if features is None:
        return

    direction_val, raw_confidence = learner.predict(features)

    ema_fast = calculate_ema(np.array(tick_buffer), EMA_FAST)
    ema_slow = calculate_ema(np.array(tick_buffer), EMA_SLOW)
    if ema_fast and ema_slow:
        ema_direction = 1 if ema_fast > ema_slow else -1
        if direction_val != ema_direction:
            raw_confidence *= 0.5

    confidence = learner.confidence_adjusted() * raw_confidence
    TRADE_AMOUNT = calculate_dynamic_stake(confidence)

    direction = "up" if direction_val == 1 else "down"

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
    record_trade_log(last_direction or "N/A", TRADE_AMOUNT, learner.confidence_adjusted(), profit)
    log_trade(last_direction or "N/A", TRADE_AMOUNT, profit)

    features = extract_features()
    if features is not None:
        learner.update(features, profit)

# ================= WEBSOCKET =================
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

# ================= MAIN LOOP =================
if __name__ == "__main__":
    console.print("[green]🚀 Alpha Momento Bot starting...[/green]")

    # Start websocket
    start_ws()

    # Start dashboard
    threading.Thread(target=dashboard_loop, daemon=True).start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        console.print("[red]Bot stopped manually[/red]")
        stop_bot = True