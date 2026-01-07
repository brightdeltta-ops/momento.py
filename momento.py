import json, time, threading, websocket, os
import numpy as np
from collections import deque
from datetime import datetime

# ============== ENV CONFIG ==============
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = os.getenv("APP_ID", "112380")
SYMBOL = os.getenv("SYMBOL", "R_75")

BASE_STAKE = float(os.getenv("BASE_STAKE", "1"))
MAX_STAKE = 100.0
TRADE_RISK_FRAC = 0.02

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOL_WINDOW = 20
VOL_THRESHOLD = 0.0015

PROPOSAL_COOLDOWN = 6
TRADE_TIMEOUT = 8
MAX_DD = 0.2

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ============== STATE ==============
ticks = deque(maxlen=500)
micro = deque(maxlen=MICRO_SLICE)

BAL = 0.0
MAX_BAL = 0.0
WINS = 0
LOSSES = 0

trade_in_progress = False
last_trade_time = 0
last_signal = None

ws = None

# ============== LOG ==============
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ============== MATH ==============
def ema(data, p):
    if len(data) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(data[-p:], w, mode="valid")[0]

def market_expanding():
    if len(ticks) < VOL_WINDOW:
        return False
    return np.std(list(ticks)[-VOL_WINDOW:]) >= VOL_THRESHOLD

def breakout_confidence():
    if len(micro) < 5:
        return None, 0.0
    arr = np.array(micro)
    hi, lo = arr.max(), arr.min()
    rng = hi - lo
    if rng < 0.002:
        return None, 0.0
    cur = arr[-1]
    std = arr.std()
    if cur >= hi - 0.2*rng:
        return "CALL", min(1, std*200)
    if cur <= lo + 0.2*rng:
        return "PUT", min(1, std*200)
    mom = cur - arr[-3]
    return ("CALL" if mom > 0 else "PUT"), min(1, abs(mom)*150)

def dynamic_stake(conf):
    risk = BAL * TRADE_RISK_FRAC
    return min(BASE_STAKE + conf*risk, MAX_STAKE)

# ============== SAFETY ==============
def watchdog():
    global trade_in_progress
    while True:
        if trade_in_progress and time.time() - last_trade_time > TRADE_TIMEOUT:
            log("⚠ TRADE TIMEOUT — RESET")
            trade_in_progress = False
        time.sleep(1)

def session_ok():
    if MAX_BAL <= 0:
        return True
    dd = (MAX_BAL - BAL) / MAX_BAL
    return dd < MAX_DD

# ============== DERIV ACTIONS ==============
def authorize():
    ws.send(json.dumps({"authorize": API_TOKEN}))

def subscribe():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))

def request_proposal(ct, stake):
    global trade_in_progress, last_trade_time
    trade_in_progress = True
    last_trade_time = time.time()
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
    log(f"📨 PROPOSAL {ct} | stake={stake:.2f}")

# ============== WS CALLBACKS ==============
def on_message(ws_, msg):
    global BAL, MAX_BAL, WINS, LOSSES, trade_in_progress, last_signal

    data = json.loads(msg)

    if "authorize" in data:
        log("AUTHORIZED")
        subscribe()

    elif "balance" in data:
        BAL = float(data["balance"]["balance"])
        MAX_BAL = max(MAX_BAL, BAL)

    elif "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        micro.append(price)

        if trade_in_progress or not session_ok() or not market_expanding():
            return

        ct, conf = breakout_confidence()
        if not ct or conf < 0.5 or ct == last_signal:
            return

        f = ema(list(micro), EMA_FAST)
        s = ema(list(micro), EMA_SLOW)
        if not f or not s:
            return
        if (ct == "CALL" and f < s) or (ct == "PUT" and f > s):
            return

        stake = dynamic_stake(conf)
        last_signal = ct
        request_proposal(ct, stake)

    elif "proposal" in data:
        ws.send(json.dumps({
            "buy": data["proposal"]["id"],
            "price": data["proposal"]["ask_price"]
        }))
        log("🔥 BUY SENT")

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            pnl = float(poc.get("profit", 0))
            trade_in_progress = False
            if pnl > 0:
                WINS += 1
            else:
                LOSSES += 1
            log(f"✔ SETTLED | P/L={pnl:.2f} | W={WINS} L={LOSSES}")

    elif "error" in data:
        log(f"❌ {data['error']['message']}")
        trade_in_progress = False

def on_open(ws_):
    authorize()

def on_close(ws_, *_):
    log("🔴 WS CLOSED — RECONNECT")
    time.sleep(2)
    start()

# ============== START ==============
def start():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_close=on_close
    )
    ws.run_forever(ping_interval=30)

if __name__ == "__main__":
    log("🚀 MOMENTO CLOUD BOT — KOYEB READY")
    threading.Thread(target=watchdog, daemon=True).start()
    start()