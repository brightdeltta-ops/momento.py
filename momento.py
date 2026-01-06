import os, json, threading, time, websocket, numpy as np
from collections import deque
from datetime import datetime, timezone

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 12

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

EMA_PULLBACK_FAST = 14
EMA_PULLBACK_SLOW = 200
ATR_PERIOD = 14

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
authorized = False

ws = None

# ================= LOG =================
def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)

# ================= UTIL =================
def ema(arr, period):
    if len(arr) < period:
        return None
    arr = np.array(arr[-period:])
    w = np.exp(np.linspace(-1, 0, period))
    w /= w.sum()
    return np.dot(arr, w)

def compute_atr(prices):
    if len(prices) < ATR_PERIOD + 1:
        return 0.5
    diffs = np.abs(np.diff(prices))
    return np.mean(diffs[-ATR_PERIOD:])

# ================= SIGNALS =================
def breakout_confidence():
    if len(tick_buffer) < 5:
        return None, 0.0
    a = np.array(tick_buffer)
    high, low = a.max(), a.min()
    rng = high - low
    if rng < 0.002:
        return None, 0.0
    cur = a[-1]
    std = a.std()
    if cur >= high - 0.2 * rng:
        return "up", min(1.0, std * 200)
    if cur <= low + 0.2 * rng:
        return "down", min(1.0, std * 200)
    momentum = cur - a[-3]
    return ("up" if momentum > 0 else "down"), min(1.0, abs(momentum) * 150)

def ema_pullback(prices):
    if len(prices) < EMA_PULLBACK_SLOW:
        return None

    ema14 = ema(prices, EMA_PULLBACK_FAST)
    ema200 = ema(prices, EMA_PULLBACK_SLOW)
    prev_price = prices[-2]
    last_price = prices[-1]
    atr = compute_atr(prices)

    if last_price > ema200 and prev_price < ema14 and last_price > ema14:
        return "up", atr
    if last_price < ema200 and prev_price > ema14 and last_price < ema14:
        return "down", atr
    return None

# ================= TRADING =================
def evaluate():
    global last_proposal_time, TRADE_AMOUNT

    if trade_in_progress:
        return
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return

    direction, conf = breakout_confidence()
    if conf < 0.5:
        return

    ef = ema(tick_buffer, EMA_FAST)
    es = ema(tick_buffer, EMA_SLOW)
    if ef is None or es is None:
        return

    trend = "up" if ef > es else "down"
    if direction != trend:
        return

    pullback = ema_pullback(list(tick_history))
    if pullback:
        direction, atr = pullback
        log(f"🔥 EMA PULLBACK {direction.upper()} ATR={atr:.4f}")

    TRADE_AMOUNT = min(MAX_STAKE, BASE_STAKE + conf * BASE_STAKE)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_queue()

def process_queue():
    global trade_in_progress, last_trade_time
    if trade_queue and not trade_in_progress:
        d, dur, s = trade_queue.popleft()
        ct = "CALL" if d == "up" else "PUT"
        ws.send(json.dumps({
            "proposal": 1,
            "amount": s,
            "basis": "stake",
            "contract_type": ct,
            "currency": "USD",
            "duration": dur,
            "duration_unit": "t",
            "symbol": SYMBOL
        }))
        trade_in_progress = True
        last_trade_time = time.time()
        log(f"📤 Proposal {ct} stake={s:.2f}")

def settle(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(c.get("profit", 0))
    BALANCE += profit
    TRADE_COUNT += 1
    WINS += profit > 0
    LOSSES += profit <= 0
    trade_in_progress = False
    log(f"💰 SETTLED P/L={profit:.2f} BAL={BALANCE:.2f}")

# ================= WS =================
def start_ws():
    global ws, authorized

    def on_open(w):
        log("Connected → Authorizing")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        global BALANCE, authorized
        d = json.loads(msg)

        if "authorize" in d:
            authorized = True
            w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            w.send(json.dumps({"balance": 1, "subscribe": 1}))
            w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
            log("✔ Authorized & Subscribed")

        if "tick" in d:
            t = float(d["tick"]["quote"])
            tick_history.append(t)
            tick_buffer.append(t)
            evaluate()

        if "proposal" in d:
            time.sleep(PROPOSAL_DELAY)
            w.send(json.dumps({"buy": d["proposal"]["id"], "price": TRADE_AMOUNT}))

        if "proposal_open_contract" in d:
            c = d["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                settle(c)

        if "balance" in d:
            BALANCE = float(d["balance"]["balance"])

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= START =================
if __name__ == "__main__":
    log("🚀 EMA BREAKOUT + PULLBACK BOT — KOYEB READY (NO pandas)")
    start_ws()

    while True:
        log("❤️ HEARTBEAT")
        time.sleep(30)