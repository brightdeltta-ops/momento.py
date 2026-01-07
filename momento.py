import json
import threading
import time
import websocket
import numpy as np
import os
from collections import deque

# =================================================
# ENV CONFIG (KOYEB SAFE)
# =================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("DERIV_APP_ID", "112380"))

if not API_TOKEN:
    raise RuntimeError("❌ DERIV_API_TOKEN not set")

SYMBOL = "R_75"
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# =================================================
# STRATEGY CONFIG
# =================================================
BASE_STAKE = 1.0
MAX_STAKE = 100.0
RISK_FRAC = 0.02
PROPOSAL_COOLDOWN = 5
EMA_FAST = 3
EMA_SLOW = 10
VOL_WINDOW = 20
VOL_THRESHOLD = 0.0015
MAX_DRAWDOWN = 0.20

# =================================================
# STATE
# =================================================
ticks = deque(maxlen=500)
micro = deque(maxlen=10)
trade_queue = deque(maxlen=5)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADES = 0

last_trade_time = 0
last_signal = None
trade_in_progress = False
authorized = False
ws_running = False
ws = None
lock = threading.Lock()

# =================================================
# LOGGING
# =================================================
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# =================================================
# MATH
# =================================================
def ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def market_expanding():
    if len(ticks) < VOL_WINDOW:
        return False
    return np.std(list(ticks)[-VOL_WINDOW:]) >= VOL_THRESHOLD

def dynamic_stake(conf):
    risk = BALANCE * RISK_FRAC
    stake = BASE_STAKE + conf * risk
    return min(stake, MAX_STAKE)

def session_ok():
    if MAX_BALANCE == 0:
        return True
    dd = (MAX_BALANCE - BALANCE) / MAX_BALANCE
    return dd < MAX_DRAWDOWN

# =================================================
# SIGNAL LOGIC
# =================================================
def breakout_signal():
    if len(micro) < 5:
        return None, 0

    arr = np.array(micro)
    hi, lo = arr.max(), arr.min()
    rng = hi - lo
    if rng < 0.002:
        return None, 0

    cur = arr[-1]
    if cur >= hi - 0.2 * rng:
        return "up", min(1, rng * 300)
    if cur <= lo + 0.2 * rng:
        return "down", min(1, rng * 300)

    mom = cur - arr[-3]
    return ("up" if mom > 0 else "down", min(1, abs(mom) * 200))

def evaluate():
    global last_trade_time, last_signal

    if trade_in_progress:
        return
    if time.time() - last_trade_time < PROPOSAL_COOLDOWN:
        return
    if not session_ok():
        log("⚠ DRAWNDOWN LIMIT — PAUSED")
        return
    if not market_expanding():
        return

    direction, conf = breakout_signal()
    if conf < 0.5:
        return
    if direction == last_signal:
        return

    f = ema(list(micro), EMA_FAST)
    s = ema(list(micro), EMA_SLOW)
    if f is None or s is None:
        return

    trend = "up" if f > s else "down"
    if trend != direction:
        return

    stake = dynamic_stake(conf)
    trade_queue.append((direction, 1, stake))
    last_trade_time = time.time()
    last_signal = direction

    log(f"🚀 SIGNAL {direction.upper()} | stake={stake:.2f} conf={conf:.2f}")
    process_queue()

# =================================================
# TRADING
# =================================================
def process_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        d, dur, amt = trade_queue.popleft()
        send_proposal(d, dur, amt)

def send_proposal(direction, duration, stake):
    global trade_in_progress
    trade_in_progress = True

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

    log(f"📨 PROPOSAL {ct} | stake={stake:.2f}")

def settle(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADES, trade_in_progress

    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)

    if profit > 0:
        WINS += 1
    else:
        LOSSES += 1

    TRADES += 1
    trade_in_progress = False

    log(f"✔ RESULT {profit:.2f} | BAL={BALANCE:.2f}")
    process_queue()

# =================================================
# WEBSOCKET
# =================================================
def on_message(ws, msg):
    global authorized, BALANCE
    data = json.loads(msg)

    if "authorize" in data:
        authorized = True
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✅ AUTHORIZED")

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        micro.append(price)
        evaluate()

    if "proposal" in data:
        pid = data["proposal"]["id"]
        ws.send(json.dumps({"buy": pid, "price": data["proposal"]["ask_price"]}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold") or c.get("is_expired"):
            settle(c)

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

def on_close(ws, *_):
    global ws_running
    log("❌ WS CLOSED — RECONNECTING")
    ws_running = False
    time.sleep(2)
    start_ws()

def on_error(ws, err):
    log(f"❌ WS ERROR: {err}")

def start_ws():
    global ws, ws_running
    if ws_running:
        return
    ws_running = True

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda w: w.send(json.dumps({"authorize": API_TOKEN})),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        try:
            ws.send(json.dumps({"ping": 1}))
        except:
            pass

# =================================================
# START
# =================================================
if __name__ == "__main__":
    log("🚀 MOMENTO BOT — KOYEB MODE")
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    while True:
        time.sleep(60)