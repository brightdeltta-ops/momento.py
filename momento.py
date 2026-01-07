import os, json, time, threading, websocket
import numpy as np
from collections import deque

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))

SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
RISK_FRAC = 0.03
MAX_MARTI = 4

EMA_FAST = 3
EMA_MED = 6
EMA_SLOW = 12

MIN_CONF = 0.65
PROPOSAL_COOLDOWN = 1.2

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= STATE =================
ticks = deque(maxlen=500)
trade_queue = deque(maxlen=20)
bias_memory = deque(maxlen=200)

BALANCE = 0.0
WINS = LOSSES = TRADES = 0

ema_fast = ema_med = ema_slow = 0.0
marti_step = 0
current_stake = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0

ws = None
lock = threading.Lock()

# ================= UTIL =================
def log(msg):
    with lock:
        print(msg, flush=True)

def ema(prev, price, period):
    alpha = 2 / (period + 1)
    return price if prev == 0 else prev * (1 - alpha) + price * alpha

# ================= MARKET INTELLIGENCE =================
def trend_direction():
    if ema_fast > ema_med > ema_slow:
        return "UP"
    if ema_fast < ema_med < ema_slow:
        return "DOWN"
    return None

def momentum_signal():
    if len(ticks) < 6:
        return None, 0
    arr = np.array(list(ticks)[-6:])
    delta = arr[-1] - arr[0]
    strength = min(abs(delta) / (np.std(arr) + 1e-6), 1)
    return ("UP" if delta > 0 else "DOWN"), strength

def volatility_ok():
    if len(ticks) < 20:
        return False
    return np.std(list(ticks)[-20:]) > 0.001

def adaptive_bias():
    score = 0
    for i, b in enumerate(reversed(bias_memory)):
        weight = 0.95 ** i
        score += weight if b == "UP" else -weight
    return score

# ================= STAKE LOGIC =================
def calc_stake(conf):
    global marti_step
    risk_cap = max(BALANCE * RISK_FRAC, BASE_STAKE)
    stake = BASE_STAKE * (1.6 ** marti_step) * (1 + conf)
    return min(stake, risk_cap, MAX_STAKE)

# ================= TRADE ENGINE =================
def evaluate_trade():
    global last_proposal_time

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not volatility_ok():
        return

    trend = trend_direction()
    mom_dir, mom_conf = momentum_signal()

    votes = {"UP": 0, "DOWN": 0}
    if trend:
        votes[trend] += 0.5
    if mom_dir:
        votes[mom_dir] += mom_conf

    bias = adaptive_bias()
    if bias > 0:
        votes["UP"] += min(bias, 0.4)
    else:
        votes["DOWN"] += min(abs(bias), 0.4)

    direction = "UP" if votes["UP"] > votes["DOWN"] else "DOWN"
    confidence = abs(votes["UP"] - votes["DOWN"])

    if confidence < MIN_CONF:
        return

    stake = calc_stake(confidence)
    trade_queue.append((direction, stake))
    last_proposal_time = time.time()

    log(f"🚀 QUEUED {direction} | Stake {stake:.2f} | Conf {confidence:.2f}")
    process_queue()

def process_queue():
    if trade_queue and not trade_in_progress:
        d, s = trade_queue.popleft()
        send_trade(d, s)

def send_trade(direction, stake):
    global trade_in_progress, last_trade_time, current_stake
    trade_in_progress = True
    last_trade_time = time.time()
    current_stake = stake

    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": "CALL" if direction == "UP" else "PUT",
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    log(f"📨 PROPOSAL SENT → {direction} | {stake:.2f}")

# ================= SETTLEMENT =================
def settle(contract):
    global BALANCE, WINS, LOSSES, TRADES, marti_step, trade_in_progress

    profit = float(contract.get("profit", 0))
    BALANCE += profit
    TRADES += 1

    direction = "UP" if contract["contract_type"] == "CALL" else "DOWN"

    if profit > 0:
        WINS += 1
        marti_step = 0
        bias_memory.append(direction)
    else:
        LOSSES += 1
        marti_step = min(marti_step + 1, MAX_MARTI)
        bias_memory.append("DOWN" if direction == "UP" else "UP")

    trade_in_progress = False
    log(f"✔ SETTLED {profit:.2f} | BAL {BALANCE:.2f}")
    process_queue()

# ================= WEBSOCKET =================
def on_message(ws, msg):
    global BALANCE
    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✔ AUTHORIZED")

    if "tick" in data:
        ticks.append(float(data["tick"]["quote"]))
        evaluate_trade()

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

    if "proposal" in data:
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": current_stake}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold") or c.get("is_expired"):
            settle(c)

def on_open(ws):
    log("🔌 CONNECTED")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_close(ws, *_):
    log("❌ DISCONNECTED → RECONNECTING")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= SAFETY =================
def watchdog():
    global trade_in_progress
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 6:
            trade_in_progress = False
            log("⚠ AUTO-UNFREEZE")
            process_queue()

def ema_engine():
    global ema_fast, ema_med, ema_slow
    while True:
        if ticks:
            p = ticks[-1]
            ema_fast = ema(ema_fast, p, EMA_FAST)
            ema_med = ema(ema_med, p, EMA_MED)
            ema_slow = ema(ema_slow, p, EMA_SLOW)
        time.sleep(0.05)

# ================= MAIN =================
if __name__ == "__main__":
    log("🚀 MOMENTO KOYEB BOT LIVE")
    start_ws()
    threading.Thread(target=ema_engine, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        time.sleep(60)