import json, threading, time, websocket, numpy as np, os
from collections import deque
from datetime import datetime

# =========================================================
# ENV (KOYEB SAFE)
# =========================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))
SYMBOL = os.getenv("SYMBOL", "R_75")

if not API_TOKEN or APP_ID == 0:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# =========================================================
# CONFIG
# =========================================================
BASE_STAKE = 1.0
MAX_STAKE = 100.0
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOL_WINDOW = 20
VOL_THRESHOLD = 0.0015
MAX_DD = 0.20
DERIV_PAYOUT = 0.95
KELLY_FRACTION = 0.35
MIN_PROB, MAX_PROB = 0.52, 0.85
EWMA_ALPHA = 0.15
MC_PATHS, MC_HORIZON, MC_BLOCK_PROB = 300, 25, 0.25
PROPOSAL_COOLDOWN = 6

# =========================================================
# STATE
# =========================================================
ticks = deque(maxlen=500)
micro = deque(maxlen=MICRO_SLICE)
queue = deque(maxlen=5)

BALANCE = 0.0
MAX_BAL = 0.0
WINS = LOSSES = TRADES = 0
STAKE = BASE_STAKE
MC_RISK = 0.0
trade_active = False
last_trade_ts = 0
ws = None

REGIME_STATS = {
    "trend": {"wins": 1, "losses": 1},
    "compression": {"wins": 1, "losses": 1},
}

EWMA = {
    "trend": {"up": 0.5, "down": 0.5},
    "compression": {"up": 0.5, "down": 0.5},
}

# =========================================================
# LOGGING
# =========================================================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# =========================================================
# CORE MATH
# =========================================================
def ema(arr, p):
    if len(arr) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.dot(arr[-p:], w)

def regime_detect():
    if len(micro) < EMA_SLOW:
        return None, 0
    v = np.std(micro)
    ef, es = ema(micro, EMA_FAST), ema(micro, EMA_SLOW)
    if ef is None or es is None:
        return None, 0
    d = abs(ef - es)
    if v > VOL_THRESHOLD and d > v * 0.3:
        return "trend", min(1, d / v)
    if v < VOL_THRESHOLD * 0.7:
        return "compression", min(1, (VOL_THRESHOLD - v) / VOL_THRESHOLD)
    return None, 0

def bayes_p(r):
    s = REGIME_STATS[r]
    return (s["wins"] + 1) / (s["wins"] + s["losses"] + 2)

def ev(p):
    return p * DERIV_PAYOUT - (1 - p)

def kelly(p):
    b, q = DERIV_PAYOUT, 1 - p
    edge = (b * p - q) / b
    return min(MAX_STAKE, max(BASE_STAKE, BALANCE * edge * KELLY_FRACTION))

def mc_dd_risk(p, stake):
    if BALANCE <= 0:
        return 1.0
    breaches = 0
    for _ in range(MC_PATHS):
        eq, peak = BALANCE, BALANCE
        for _ in range(MC_HORIZON):
            eq += stake * DERIV_PAYOUT if np.random.rand() < p else -stake
            peak = max(peak, eq)
            if (peak - eq) / peak >= MAX_DD:
                breaches += 1
                break
    return breaches / MC_PATHS

# =========================================================
# DECISION ENGINE
# =========================================================
def evaluate():
    global STAKE, MC_RISK, last_trade_ts
    if trade_active or time.time() - last_trade_ts < PROPOSAL_COOLDOWN:
        return

    r, conf = regime_detect()
    if not r or conf < 0.45:
        return

    direction = "up" if ema(micro, EMA_FAST) > ema(micro, EMA_SLOW) else "down"
    p = np.clip(
        0.5 * bayes_p(r) + 0.3 * EWMA[r][direction] + 0.2 * conf,
        MIN_PROB, MAX_PROB
    )

    if ev(p) <= 0:
        return

    STAKE = kelly(p)
    MC_RISK = mc_dd_risk(p, STAKE)
    if MC_RISK > MC_BLOCK_PROB:
        log(f"MC BLOCK | risk={MC_RISK:.2f}")
        return

    queue.append((direction, r, STAKE))
    last_trade_ts = time.time()
    send_next()

# =========================================================
# TRADING
# =========================================================
def send_next():
    global trade_active
    if not trade_active and queue:
        d, r, s = queue.popleft()
        ws.send(json.dumps({
            "proposal": 1,
            "amount": s,
            "basis": "stake",
            "contract_type": "CALL" if d == "up" else "PUT",
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",
            "symbol": SYMBOL
        }))
        trade_active = True
        log(f"SEND {r.upper()} {d.upper()} stake={s:.2f}")

def settle(c):
    global BALANCE, MAX_BAL, WINS, LOSSES, TRADES, trade_active
    p = float(c.get("profit", 0))
    BALANCE += p
    MAX_BAL = max(MAX_BAL, BALANCE)
    win = p > 0
    r = "trend" if abs(ema(micro, EMA_FAST) - ema(micro, EMA_SLOW)) > 0 else "compression"
    if win:
        WINS += 1
        REGIME_STATS[r]["wins"] += 1
    else:
        LOSSES += 1
        REGIME_STATS[r]["losses"] += 1
    TRADES += 1
    trade_active = False
    log(f"{'WIN' if win else 'LOSS'} {p:.2f} BAL={BALANCE:.2f}")
    send_next()

# =========================================================
# WEBSOCKET
# =========================================================
def on_message(_, msg):
    data = json.loads(msg)
    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("AUTHORIZED")

    if "tick" in data:
        q = float(data["tick"]["quote"])
        ticks.append(q)
        micro.append(q)
        evaluate()

    if "proposal" in data:
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": STAKE}))

    if "proposal_open_contract" in data:
        if data["proposal_open_contract"].get("is_sold"):
            settle(data["proposal_open_contract"])

    if "balance" in data:
        global BALANCE
        BALANCE = float(data["balance"]["balance"])

def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda w: w.send(json.dumps({"authorize": API_TOKEN})),
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# =========================================================
# HEARTBEAT
# =========================================================
def heartbeat():
    while True:
        time.sleep(30)
        log(f"❤️ BAL={BALANCE:.2f} W={WINS} L={LOSSES} MC={MC_RISK:.2f}")

# =========================================================
# START
# =========================================================
def main():
    log("🚀 MONTE-CARLO ALPHA BOT — KOYEB READY")
    start_ws()
    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(10)

if __name__ == "__main__":
    main()