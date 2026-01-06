import json, threading, time, websocket, numpy as np, os
from collections import deque
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

# ================= USER CONFIG =================
API_TOKEN = os.getenv("API_TOKEN")  # Koyeb env var
APP_ID = int(os.getenv("APP_ID", "112380"))
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
COMPRESSION_BASE = 0.003
MEAN_REVERT_ATR_MULT = 1.0
ATR_PERIOD = 14
EMA_REGIME_SLOW = 30
EMA_PULLBACK_FAST = 14
EMA_PULLBACK_SLOW = 50

# ================= DATA TRACKING =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)
active_trades = []

BALANCE = 1000.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
authorized = False
ws_running = False
ws = None
lock = threading.Lock()
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

alerts = deque(maxlen=50)
equity_curve = []

console = Console()

# ================= Per-Regime Stats =================
regime_stats = {
    "Trending": {"wins":0,"losses":0,"profit":0,"trades":0,"streak":0},
    "Ranging": {"wins":0,"losses":0,"profit":0,"trades":0,"streak":0},
    "Spike": {"wins":0,"losses":0,"profit":0,"trades":0,"streak":0}
}

# ================= UTILITIES =================
def append_alert(msg, color="white"):
    with lock:
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
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1.,0.,period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def compute_atr(prices, period=ATR_PERIOD):
    if len(prices) < period+1:
        return 0.001
    tr = [abs(prices[i]-prices[i-1]) for i in range(1,len(prices))]
    return np.mean(tr[-period:])

def session_loss_check():
    return LOSSES*BASE_STAKE < 50

# ================= REGIME DETECTION =================
def detect_regime():
    if len(tick_history) < EMA_REGIME_SLOW:
        return "Ranging"
    atr = compute_atr(list(tick_history))
    slow_ema = calculate_ema(list(tick_history), EMA_REGIME_SLOW)
    prev_slow_ema = calculate_ema(list(tick_history)[:-1], EMA_REGIME_SLOW)
    if slow_ema is None or prev_slow_ema is None:
        return "Ranging"

    slope = slow_ema - prev_slow_ema
    if abs(slope) > atr*0.1:
        return "Trending"
    if atr > 0.0025:
        return "Spike"
    return "Ranging"

# ================= SIGNALS =================
def ema_pullback_signal(prices):
    if len(prices) < EMA_PULLBACK_SLOW:
        return None
    df = pd.DataFrame({"Close": prices})
    df["EMA14"] = df["Close"].ewm(span=EMA_PULLBACK_FAST, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=EMA_PULLBACK_SLOW, adjust=False).mean()
    last = df.iloc[-1]
    prev = df.iloc[-2]
    atr = compute_atr(prices)
    if last["Close"] > df["EMA50"].iloc[-1] and prev["Close"] < df["EMA14"].iloc[-2] and last["Close"] > df["EMA14"].iloc[-1]:
        return "up", last["Close"] - atr, last["Close"] + 2*atr
    elif last["Close"] < df["EMA50"].iloc[-1] and prev["Close"] > df["EMA14"].iloc[-2] and last["Close"] < df["EMA14"].iloc[-1]:
        return "down", last["Close"] + atr, last["Close"] - 2*atr
    return None

def get_compression_breakout():
    if len(tick_buffer) < MICRO_SLICE:
        return None,0
    recent = np.array(tick_buffer)
    rng = recent.max() - recent.min()
    dynamic_thresh = COMPRESSION_BASE * (1 + compute_atr(list(tick_history))*50)
    if rng < dynamic_thresh:
        current = recent[-1]
        direction = None
        if current > recent.max(): direction="up"
        elif current < recent.min(): direction="down"
        confidence = min(1, rng*200+0.2) if direction else 0
        return direction, confidence
    return None,0

def get_mean_reversion():
    if len(tick_history) < 14:
        return None,0
    last_price = tick_history[-1]
    ema14 = calculate_ema(list(tick_history), 14)
    atr = compute_atr(list(tick_history))
    if ema14 is None: return None,0
    if last_price > ema14 + MEAN_REVERT_ATR_MULT*atr:
        return "down", 0.6
    elif last_price < ema14 - MEAN_REVERT_ATR_MULT*atr:
        return "up", 0.6
    return None,0

def get_dynamic_sl_tp(direction, price, atr, regime):
    if regime=="Trending":
        sl = price - atr if direction=="up" else price + atr
        tp = price + 2*atr if direction=="up" else price - 2*atr
    elif regime=="Ranging":
        sl = price - 0.5*atr if direction=="up" else price + 0.5*atr
        tp = price + atr if direction=="up" else price - atr
    else:
        sl = price - atr if direction=="up" else price + atr
        tp = price + 1.5*atr if direction=="up" else price - 1.5*atr
    return sl,tp

# ================= TRADE EXECUTION =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        return

    regime = detect_regime()
    direction, confidence, signal_color = None, 0, "white"

    # Regime-specific signals
    if regime == "Trending":
        direction, confidence = get_compression_breakout()
        if direction: signal_color = "green" if direction=="up" else "red"
        elif ema_pullback_signal(list(tick_history)):
            direction, sl, tp = ema_pullback_signal(list(tick_history))
            signal_color = "cyan"
    elif regime == "Ranging":
        direction, confidence = get_mean_reversion()
        signal_color = "yellow" if direction else "white"
        if not direction:
            direction, confidence = get_compression_breakout()
            if direction: signal_color = "green" if direction=="up" else "red"
    elif regime == "Spike":
        direction, confidence = get_compression_breakout()
        if direction: signal_color = "green" if direction=="up" else "red"

    if direction:
        if regime=="Trending": TRADE_AMOUNT = min(MAX_STAKE, BASE_STAKE + confidence*BASE_STAKE*2)
        elif regime=="Spike": TRADE_AMOUNT = min(MAX_STAKE, BASE_STAKE + confidence*BASE_STAKE*1.5)
        else: TRADE_AMOUNT = BASE_STAKE

        duration = max(MIN_DURATION, int((1-confidence)*MAX_DURATION))
        trade_queue.append((direction, duration, TRADE_AMOUNT, regime, signal_color))
        last_proposal_time = time.time()
        append_alert(f"🚀 Queued {direction.upper()} | Stake {TRADE_AMOUNT:.2f} | Dur {duration} | Conf {confidence:.2f} | Regime {regime}", signal_color)
        process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        sig = trade_queue.popleft()
        request_proposal(sig)

def request_proposal(sig):
    global TRADE_AMOUNT, trade_in_progress, last_trade_time
    direction,duration,stake,regime,color = sig
    TRADE_AMOUNT = stake
    ct = "CALL" if direction=="up" else "PUT"
    proposal = {
        "proposal":1,
        "amount":TRADE_AMOUNT,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":duration,
        "duration_unit":"t",
        "symbol":SYMBOL
    }
    trade_in_progress = True
    last_trade_time = time.time()
    ws.send(json.dumps(proposal))
    append_alert(f"📨 Proposal sent {direction.upper()} | Stake {TRADE_AMOUNT:.2f} | Dur {duration} | Regime {regime}", color)

def on_contract_settlement(c, regime):
    global BALANCE,WINS,LOSSES,TRADE_COUNT,trade_in_progress
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    TRADE_COUNT += 1
    trade_in_progress = False

    stats = regime_stats[regime]
    stats["trades"] += 1
    stats["profit"] += profit
    if profit>0:
        WINS += 1
        stats["wins"] += 1
        stats["streak"] += 1
    else:
        LOSSES += 1
        stats["losses"] += 1
        stats["streak"] = 0

    append_alert(f"✔ Settlement → Profit {profit:.2f} | Regime {regime}", "green")
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws,msg):
    global BALANCE,authorized
    try:
        data=json.loads(msg)
        if "authorize" in data:
            authorized=True
            resubscribe_channels()
            append_alert("✔ Authorized", "green")
        if "tick" in data:
            tick=float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            evaluate_and_trade()
        if "proposal" in data:
            pid=data["proposal"]["id"]
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy":pid,"price":TRADE_AMOUNT}))
            active_trades.append({"id":pid,"profit":0})
        if "proposal_open_contract" in data:
            c=data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                regime = detect_regime()
                on_contract_settlement(c, regime)
                active_trades[:] = [t for t in active_trades if t["id"]!=c.get("id")]
        if "balance" in data:
            BALANCE=float(data["balance"]["balance"])
            equity_curve.append(BALANCE)
    except Exception as e:
        append_alert(f"❌ WS Error: {e}", "red")

def resubscribe_channels():
    if not authorized: return
    ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
    ws.send(json.dumps({"balance":1,"subscribe":1}))
    ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
    append_alert("🔄 Resubscribed channels", "green")

def on_error(ws,error):
    append_alert(f"❌ WS Error: {error}", "red")

def on_close(ws,code,msg):
    append_alert("❌ WS Closed — reconnecting in 2s", "yellow")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws, ws_running, authorized
    if ws_running: return
    ws_running=True
    authorized=False
    ws=websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda ws: ws.send(json.dumps({"authorize":API_TOKEN})),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever,daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        try: ws.send(json.dumps({"ping":1}))
        except: pass

# ================= START =================
def start():
    append_alert("🚀 Alpha Bot starting...", "cyan")
    start_ws()
    threading.Thread(target=keep_alive,daemon=True).start()
    threading.Thread(target=auto_unfreeze,daemon=True).start()
    while True:
        time.sleep(1)

if __name__=="__main__":
    start()