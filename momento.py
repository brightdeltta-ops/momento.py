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
TRADE_RISK_FRAC = 0.05
PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOLATILITY_WINDOW = 15
MAX_DD = 0.15
MIN_SHARPE = 0.2

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=50)
trade_log = deque(maxlen=10)
ROLLING_PNL = deque(maxlen=50)

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

WIN_STREAK = 0
LOSS_STREAK = 0

ws = None
console = Console()

# ================= ALPHA LEARNER =================
class AlphaLearner:
    def __init__(self, n_features):
        self.weights = np.zeros(n_features)
        self.bias = 0.0
        self.lr = 0.1
        self.trade_history = deque(maxlen=20)

    def predict(self, x):
        score = np.dot(self.weights, x) + self.bias
        confidence = min(max(0.5, abs(score)), 1.0)
        return 1 if score > 0 else -1, confidence

    def update(self, x, profit):
        y = 1 if profit > 0 else -1
        pred, _ = self.predict(x)
        error = y - pred
        self.weights += self.lr * error * x * max(abs(profit), 0.5)
        self.bias += self.lr * error * max(abs(profit), 0.5)
        self.trade_history.append(y)

    def rolling_accuracy(self):
        if not self.trade_history:
            return 0.5
        return sum([1 if t > 0 else 0 for t in self.trade_history]) / len(self.trade_history)

learner = AlphaLearner(n_features=8)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def session_loss_check():
    return (MAX_BALANCE - BALANCE) < (MAX_BALANCE * MAX_DD)

def extract_features():
    if len(tick_buffer) < MICRO_SLICE:
        return None
    arr = np.array(tick_buffer)
    ema_fast = calculate_ema(arr, EMA_FAST)
    ema_slow = calculate_ema(arr, EMA_SLOW)
    ema_slope = ema_fast - ema_slow
    tick_range = arr[-1] - arr[0]
    volatility = arr.std()
    momentum = arr[-1] - arr[-3]
    atr_like = np.mean(np.abs(np.diff(arr)))
    mean_tick = arr.mean()
    return np.array([
        ema_fast, ema_slow, ema_slope,
        tick_range, volatility, momentum,
        atr_like, mean_tick
    ])

def adaptive_volatility_threshold():
    recent_std = np.std(list(tick_history)[-VOLATILITY_WINDOW:])
    return max(0.0003, recent_std * 0.7)

def rolling_sharpe():
    if not ROLLING_PNL:
        return 0.0
    pnl_array = np.array(ROLLING_PNL)
    return pnl_array.mean() / (pnl_array.std() + 1e-6)

def calculate_streak_multiplier():
    multiplier = 1.0
    if WIN_STREAK >= 2:
        multiplier += 0.2
    elif LOSS_STREAK >= 2:
        multiplier -= 0.3
    return max(0.5, multiplier)

def calculate_dynamic_stake_ultra(confidence):
    base = BASE_STAKE + confidence * TRADE_RISK_FRAC * BALANCE * max(0.3, 1 - ((MAX_BALANCE - BALANCE) / (MAX_BALANCE + 1e-6)))
    streak_mult = calculate_streak_multiplier()
    sharpe_mult = 1.0 if rolling_sharpe() >= MIN_SHARPE else 0.5
    return min(base * streak_mult * sharpe_mult, MAX_STAKE)

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

# ================= TRADING =================
def evaluate_and_trade_ultra():
    global last_proposal_time, TRADE_AMOUNT, last_direction
    if stop_bot or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check() or len(tick_history) < VOLATILITY_WINDOW:
        return
    if np.std(list(tick_history)[-VOLATILITY_WINDOW:]) < adaptive_volatility_threshold():
        return

    features = extract_features()
    if features is None:
        return

    direction_num, confidence = learner.predict(features)
    direction = "up" if direction_num == 1 else "down"
    TRADE_AMOUNT = calculate_dynamic_stake_ultra(confidence)
    trade_queue.append((confidence, direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    last_direction = direction
    process_trade_queue_priority()

def process_trade_queue_priority():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        sorted_queue = sorted(trade_queue, key=lambda x: -x[0])
        _, direction, duration, stake = sorted_queue.pop(0)
        trade_queue.clear()
        for t in sorted_queue:
            trade_queue.append(t)
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

def on_contract_settlement_ultra(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress, MAX_BALANCE
    global WIN_STREAK, LOSS_STREAK
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    WINS += profit > 0
    LOSSES += profit <= 0
    TRADE_COUNT += 1
    trade_in_progress = False
    record_trade_log(last_direction or "N/A", TRADE_AMOUNT, 0.7, profit)

    # Update streaks
    if profit > 0:
        WIN_STREAK += 1
        LOSS_STREAK = 0
    else:
        LOSS_STREAK += 1
        WIN_STREAK = 0

    ROLLING_PNL.append(profit)
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

    def on_message(ws_app, msg):
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
                evaluate_and_trade_ultra()
            if "proposal" in data:
                time.sleep(PROPOSAL_DELAY)
                ws.send(json.dumps({"buy": data["proposal"]["id"], "price": TRADE_AMOUNT}))
            if "proposal_open_contract" in data:
                c = data["proposal_open_contract"]
                if c.get("is_sold") or c.get("is_expired"):
                    on_contract_settlement_ultra(c)
            if "balance" in data:
                global BALANCE
                BALANCE = float(data["balance"]["balance"])
        except Exception as e:
            console.log(f"[red]on_message error: {e}[/red]")

    def on_error(ws_app, error):
        console.log(f"[red]WebSocket ERROR: {error}[/red]")

    def on_close(ws_app, code, msg):
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
def dashboard_loop():
    with Live(auto_refresh=True, refresh_per_second=1) as live:
        last_trade_count = -1
        while True:
            table = Table(title="🚀 Momento Bot Dashboard [ULTRA-ALPHA]")
            table.add_column("Metric", justify="left")
            table.add_column("Value", justify="right")

            table.add_row("Balance", f"{BALANCE:.2f}")
            table.add_row("Max Balance", f"{MAX_BALANCE:.2f}")
            table.add_row("Trades", str(TRADE_COUNT))
            table.add_row("Wins", str(WINS))
            table.add_row("Losses", str(LOSSES))
            table.add_row("Learner Accuracy", f"{learner.rolling_accuracy()*100:.1f}%")
            table.add_row("Rolling Sharpe", f"{rolling_sharpe():.2f}")
            table.add_row("Win Streak", str(WIN_STREAK))
            table.add_row("Loss Streak", str(LOSS_STREAK))

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

# ================= START =================
if __name__ == "__main__":
    console.print("[green]🚀 Momento Bot starting on Koyeb [ULTRA-ALPHA][/green]")
    start_ws()
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        time.sleep(5)