import os, json, time, threading, websocket, numpy as np
from collections import deque
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

SYMBOL = "R_75"

BASE_STAKE = 2.0
EMA_FAST = 5
EMA_SLOW = 15
TICKS_PER_CANDLE = 10
BREAKOUT_LOOKBACK = 25

# Kelly
KELLY_FRACTION = 0.3
KELLY_CAP = 0.05
MIN_KELLY_TRADES = 12

# Bayesian
BAYES_ALPHA = 2.0
BAYES_BETA = 2.0

# Engine safety
ENGINE_WINDOW = 30
ENGINE_MIN_TRADES = 10
ENGINE_MAX_DD = -0.12
ENGINE_COOLDOWN = 300

# ================= STATE =================
tick_history = deque(maxlen=1000)
candle_buffer = deque(maxlen=300)
current_candle = []

BALANCE = 0.0
WINS = 0
LOSSES = 0

trade_queue = deque()
OPEN_CONTRACT = None
last_trade_time = 0

ws = None
lock = threading.Lock()

# ================= ENGINE STATE =================
engine_state = {
    k: {
        "enabled": True,
        "pnl": deque(maxlen=ENGINE_WINDOW),
        "alpha": BAYES_ALPHA,
        "beta": BAYES_BETA,
        "disabled_at": None
    }
    for k in ["Momentum", "Compression", "Breakout"]
}

# ================= LOGGING =================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ================= UTIL =================
def ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def build_candle(price):
    global current_candle
    current_candle.append(price)
    if len(current_candle) >= TICKS_PER_CANDLE:
        o, h, l, c = current_candle[0], max(current_candle), min(current_candle), current_candle[-1]
        candle_buffer.append((o, h, l, c))
        current_candle = []
        return True
    return False

# ================= REGIME =================
def detect_market_regime():
    if len(candle_buffer) < 30:
        return "idle", 0.0

    closes = np.array([c[3] for c in candle_buffer])
    returns = np.diff(closes)
    vol = np.std(returns)

    ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    if ef is None or es is None:
        return "idle", 0.0

    diff = abs(ef - es)

    if diff > vol * 1.2:
        return "trend", min(1.0, diff / (vol + 1e-6))

    if vol < np.percentile(np.abs(returns), 30):
        return "compression", min(1.0, (0.002 - vol) / 0.002)

    return "idle", 0.0

# ================= ENGINES =================
def momentum_engine():
    closes = np.array([c[3] for c in candle_buffer])
    ef, es = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    if ef is None or es is None:
        return None, 0.0
    d = ef - es
    return ("up" if d > 0 else "down"), min(abs(d) * 50, 1.0)

def compression_engine():
    recent = candle_buffer[-20:]
    highs, lows = [c[1] for c in recent], [c[2] for c in recent]
    rng = max(highs) - min(lows)
    if rng < 0.002:
        return None, 0.0
    close = recent[-1][3]
    edge = min(rng * 100, 1.0)
    if close >= max(highs) - 0.15 * rng:
        return "down", edge
    if close <= min(lows) + 0.15 * rng:
        return "up", edge
    return None, 0.0

def breakout_engine():
    if len(candle_buffer) < BREAKOUT_LOOKBACK + 2:
        return None, 0.0
    prev = list(candle_buffer)[-BREAKOUT_LOOKBACK:-1]
    last = candle_buffer[-1]
    hi, lo = max(c[1] for c in prev), min(c[2] for c in prev)
    rng = hi - lo
    if rng < 0.002:
        return None, 0.0
    if last[3] > hi:
        return "up", min(rng * 120, 1.0)
    if last[3] < lo:
        return "down", min(rng * 120, 1.0)
    return None, 0.0

ENGINES = {
    "Momentum": momentum_engine,
    "Compression": compression_engine,
    "Breakout": breakout_engine
}

# ================= RISK =================
def bayes_win(engine):
    s = engine_state[engine]
    return s["alpha"] / (s["alpha"] + s["beta"])

def kelly(engine, balance):
    pnl = list(engine_state[engine]["pnl"])
    if len(pnl) < MIN_KELLY_TRADES:
        return BASE_STAKE
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p < 0]
    if not wins or not losses:
        return BASE_STAKE
    wr = bayes_win(engine)
    edge = wr * np.mean(wins) - (1 - wr) * np.mean(losses)
    var = np.mean(np.abs(pnl)) + 1e-6
    frac = max(0.0, edge / var) * KELLY_FRACTION
    return min(balance * frac, balance * KELLY_CAP)

# ================= TRADING =================
def evaluate_trades():
    global last_trade_time
    regime, conf = detect_market_regime()
    if regime == "idle" or conf < 0.4:
        return

    trades, total_edge = [], 0
    for name, fn in ENGINES.items():
        if not engine_state[name]["enabled"]:
            continue
        d, e = fn()
        if d and e > 0.3:
            trades.append((name, d, e))
            total_edge += e

    if not trades or total_edge < 1.3:
        return

    for name, d, e in trades:
        stake = kelly(name, BALANCE) * (e / total_edge)
        trade_queue.append((name, d, stake))

    process_trade()

def process_trade():
    global OPEN_CONTRACT
    if OPEN_CONTRACT or not trade_queue:
        return

    name, d, stake = trade_queue.popleft()
    ct = "CALL" if d == "up" else "PUT"

    ws.send(json.dumps({
        "proposal": 1,
        "amount": round(stake, 2),
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 5,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

    OPEN_CONTRACT = name
    log(f"📤 Proposal {name} {ct} stake={stake:.2f}")

# ================= WS =================
def start_ws():
    global ws

    def on_open(w):
        log("Connected → Authorizing")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        global BALANCE, WINS, LOSSES, OPEN_CONTRACT

        d = json.loads(msg)

        if "authorize" in d:
            w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
            w.send(json.dumps({"balance": 1, "subscribe": 1}))

        if "tick" in d:
            p = float(d["tick"]["quote"])
            tick_history.append(p)
            if build_candle(p):
                evaluate_trades()

        if "proposal" in d:
            w.send(json.dumps({"buy": d["proposal"]["id"], "price": d["proposal"]["ask_price"]}))

        if "proposal_open_contract" in d:
            c = d["proposal_open_contract"]
            if c.get("is_sold"):
                profit = float(c.get("profit", 0))
                BALANCE += profit
                engine = OPEN_CONTRACT
                OPEN_CONTRACT = None

                if engine in engine_state:
                    engine_state[engine]["pnl"].append(profit)
                    if profit > 0:
                        engine_state[engine]["alpha"] += 1
                        WINS += 1
                    else:
                        engine_state[engine]["beta"] += 1
                        LOSSES += 1

                log(f"💰 {engine} P/L={profit:.2f} BAL={BALANCE:.2f}")

        if "balance" in d:
            BALANCE = float(d["balance"]["balance"])

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= START =================
if __name__ == "__main__":
    log("🚀 Bayesian Kelly Bot — KOYEB READY")
    start_ws()

    while True:
        log("❤️ HEARTBEAT")
        time.sleep(30)