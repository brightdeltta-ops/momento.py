import json, threading, time, websocket, numpy as np, os, pickle
from collections import deque
from rich.console import Console
from rich.table import Table
from rich.live import Live
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
TRADE_RISK_FRAC = 0.02
PROPOSAL_COOLDOWN = 2
PROPOSAL_DELAY = 6
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
MAX_DURATION = 12
MIN_DURATION = 1
ATR_PERIOD = 14
EMA_REGIME_SLOW = 30
EMA_PULLBACK_FAST = 14
EMA_PULLBACK_SLOW = 50
MEAN_REVERT_ATR_MULT = 1.0

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)
active_trades = []

BALANCE = 1000.0
TRADE_AMOUNT = BASE_STAKE
WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
last_direction = None

ws = None
console = Console()
alerts = deque(maxlen=50)
equity_curve = []

# ================= UTILITIES =================
def append_alert(msg, color="white"):
    alerts.appendleft((msg, color))
    console.log(f"[{color}]{msg}[/{color}]")

def auto_unfreeze():
    global trade_in_progress
    while True:
        time.sleep(2)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            append_alert("⚠ Auto-unfreeze triggered", "yellow")

def calculate_ema(data, period):
    if len(data) < period: return None
    w = np.exp(np.linspace(-1.,0.,period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def compute_atr(prices, period=ATR_PERIOD):
    if len(prices) < period+1: return 0.001
    tr = [abs(prices[i]-prices[i-1]) for i in range(1,len(prices))]
    return np.mean(tr[-period:])

def detect_regime():
    if len(tick_history) < EMA_REGIME_SLOW: return "Ranging"
    atr = compute_atr(list(tick_history))
    slow_ema = calculate_ema(list(tick_history), EMA_REGIME_SLOW)
    prev_ema = calculate_ema(list(tick_history)[:-1], EMA_REGIME_SLOW)
    if slow_ema is None or prev_ema is None: return "Ranging"
    slope = slow_ema - prev_ema
    if abs(slope) > atr*0.1: return "Trending"
    if atr > 0.0025: return "Spike"
    return "Ranging"

# ================= SIGNALS =================
def ema_pullback_signal(prices):
    if len(prices) < EMA_PULLBACK_SLOW: return None
    df = pd.DataFrame({"Close": prices})
    df["EMA14"] = df["Close"].ewm(span=EMA_PULLBACK_FAST, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=EMA_PULLBACK_SLOW, adjust=False).mean()
    last, prev = df.iloc[-1], df.iloc[-2]
    atr = compute_atr(prices)
    if last["Close"] > df["EMA50"].iloc[-1] and prev["Close"] < df["EMA14"].iloc[-2] and last["Close"] > df["EMA14"].iloc[-1]:
        return "up"
    if last["Close"] < df["EMA50"].iloc[-1] and prev["Close"] > df["EMA14"].iloc[-2] and last["Close"] < df["EMA14"].iloc[-1]:
        return "down"
    return None

def get_mean_reversion():
    if len(tick_history) < 14: return None
    last = tick_history[-1]
    ema14 = calculate_ema(list(tick_history), 14)
    atr = compute_atr(list(tick_history))
    if last > ema14 + MEAN_REVERT_ATR_MULT*atr: return "down"
    if last < ema14 - MEAN_REVERT_ATR_MULT*atr: return "up"
    return None

def get_compression_breakout():
    if len(tick_buffer) < MICRO_SLICE: return None
    arr = np.array(tick_buffer)
    rng = arr.max() - arr.min()
    threshold = 0.003 * (1 + compute_atr(list(tick_history))*50)
    if rng < threshold:
        current = arr[-1]
        if current > arr.max(): return "up"
        if current < arr.min(): return "down"
    return None

def dynamic_stake(conf):
    stake = BASE_STAKE + conf * BALANCE * TRADE_RISK_FRAC
    return min(max(stake, BASE_STAKE), MAX_STAKE)

# ================= TRADING =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_direction
    if trade_in_progress: return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN: return
    regime = detect_regime()
    direction = None
    if regime == "Trending": direction = get_compression_breakout() or ema_pullback_signal(list(tick_history))
    if regime == "Ranging": direction = get_mean_reversion() or get_compression_breakout()
    if regime == "Spike": direction = get_compression_breakout()
    if not direction: return

    TRADE_AMOUNT = dynamic_stake(0.5)
    duration = MAX_DURATION
    trade_queue.append((direction, duration, TRADE_AMOUNT))
    last_direction = direction
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, s = trade_queue.popleft()
        send_proposal(d, dur, s)

def send_proposal(d, dur, s):
    global trade_in_progress
    ct = "CALL" if d=="up" else "PUT"
    ws.send(json.dumps({
        "proposal":1,
        "amount":s,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":dur,
        "duration_unit":"t",
        "symbol":SYMBOL
    }))
    append_alert(f"📨 Proposal sent {ct} | Stake {s:.2f}", "magenta")
    trade_in_progress = True

def settle(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(c.get("profit",0))
    BALANCE += profit
    TRADE_COUNT += 1
    trade_in_progress = False
    if profit>0: WINS+=1
    else: LOSSES+=1
    append_alert(f"✔ Settled {last_direction} | Profit {profit:.2f}", "green" if profit>0 else "red")

# ================= WEBSOCKET =================
def resubscribe():
    ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
    ws.send(json.dumps({"balance":1,"subscribe":1}))
    ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))

def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

    def on_open(w): w.send(json.dumps({"authorize":API_TOKEN}))
    def on_message(w,msg):
        try:
            data = json.loads(msg)
            if "authorize" in data: resub()
            if "tick" in data:
                t = float(data["tick"]["quote"])
                tick_history.append(t)
                tick_buffer.append(t)
                append_alert(f"💠 Tick {t:.4f}", "cyan")
                evaluate_and_trade()
            if "proposal" in data:
                time.sleep(PROPOSAL_DELAY)
                w.send(json.dumps({"buy":data["proposal"]["id"],"price":TRADE_AMOUNT}))
            if "proposal_open_contract" in data:
                c = data["proposal_open_contract"]
                if c.get("is_sold") or c.get("is_expired"): settle(c)
            if "balance" in data:
                global BALANCE
                BALANCE = float(data["balance"]["balance"])
        except Exception as e:
            append_alert(f"❌ WS Error {e}", "red")

    ws = websocket.WebSocketApp(url,on_open=on_open,on_message=on_message)
    threading.Thread(target=ws.run_forever,daemon=True).start()

# ================= DASHBOARD =================
def dashboard():
    with Live(refresh_per_second=1) as live:
        last_trade = -1
        while True:
            t = Table(title="🚀 ALPHA BOT — Koyeb Ready", box=None)
            t.add_column("Metric")
            t.add_column("Value")
            t.add_row("Balance", f"{BALANCE:.2f}")
            t.add_row("Trades", str(TRADE_COUNT))
            t.add_row("Wins", str(WINS))
            t.add_row("Losses", str(LOSSES))
            t.add_row("Open Trades", str(len(trade_queue)))
            live.update(t)
            time.sleep(1)

# ================= START =================
if __name__=="__main__":
    append_alert("🚀 ALPHA BOT STARTED — Koyeb Compatible", "green")
    start_ws()
    threading.Thread(target=dashboard,daemon=True).start()
    threading.Thread(target=auto_unfreeze,daemon=True).start()
    while True: time.sleep(5)