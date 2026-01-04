import json, threading, time, websocket, os, numpy as np, logging
from collections import deque
from datetime import datetime

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("MOMENTO")

# ================= ENV =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID")

# ================= CONFIG =================
SYMBOL = "R_75"
MAX_DD = 0.25
COOLDOWN = 3
DURATION = 1

EMA_FAST = 3
EMA_SLOW = 10
VOL_WINDOW = 20

# ================= COMPOUNDING =================
COMPOUNDING_TABLE = [
    (50, 0.35, "SAFE"),
    (100, 0.50, "SAFE"),
    (200, 0.75, "NORMAL"),
    (400, 1.00, "NORMAL"),
    (800, 1.50, "AGGRESSIVE"),
    (1500, 2.00, "AGGRESSIVE"),
]

# ================= STATE =================
ticks = deque(maxlen=500)
balance = 0.0
max_balance = 0.0
last_trade = 0
trade_active = False
ws = None

# ================= HELPERS =================
def ema(arr, period):
    if len(arr) < period:
        return None
    w = np.exp(np.linspace(-1, 0, period))
    w /= w.sum()
    return np.dot(arr[-period:], w)

def get_compound(balance):
    stake, mode = COMPOUNDING_TABLE[0][1], COMPOUNDING_TABLE[0][2]
    for lvl, s, m in COMPOUNDING_TABLE:
        if balance >= lvl:
            stake, mode = s, m
    return stake, mode

def regime_filter():
    if len(ticks) < VOL_WINDOW:
        return None

    arr = np.array(list(ticks)[-VOL_WINDOW:])
    vol = arr.std()

    if vol < 0.25:
        return None
    elif vol < 0.6:
        return "NORMAL"
    else:
        return "AGGRESSIVE"

def drawdown_ok():
    return balance >= max_balance * (1 - MAX_DD)

# ================= TRADING =================
def evaluate():
    global last_trade, trade_active

    if trade_active or time.time() - last_trade < COOLDOWN:
        return
    if not drawdown_ok():
        log.warning("🛑 Drawdown limit hit")
        return

    regime = regime_filter()
    if not regime:
        return

    arr = np.array(list(ticks))
    ef, es = ema(arr, EMA_FAST), ema(arr, EMA_SLOW)
    if ef is None or es is None:
        return

    direction = "CALL" if ef > es else "PUT"
    stake, mode = get_compound(balance)

    if regime == "NORMAL" and mode == "AGGRESSIVE":
        stake *= 0.7

    send_trade(direction, stake, regime)

def send_trade(direction, stake, regime):
    global trade_active, last_trade

    log.info(f"📤 TRADE | {direction} | ${stake:.2f} | REGIME={regime}")
    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": DURATION,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    trade_active = True
    last_trade = time.time()

# ================= WS =================
def start_ws():
    global ws

    def on_open(w):
        log.info("✅ Connected to Deriv WebSocket")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        global balance, max_balance, trade_active
        data = json.loads(msg)

        if "authorize" in data:
            w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            w.send(json.dumps({"balance": 1, "subscribe": 1}))
            w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

        if "tick" in data:
            price = float(data["tick"]["quote"])
            ticks.append(price)
            evaluate()

        if "proposal" in data:
            w.send(json.dumps({"buy": data["proposal"]["id"], "price": data["proposal"]["ask_price"]}))

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold"):
                profit = float(c.get("profit", 0))
                balance += profit
                max_balance = max(max_balance, balance)
                trade_active = False
                log.info(f"💰 RESULT | Profit={profit:.2f} | Balance={balance:.2f}")

        if "balance" in data:
            balance = float(data["balance"]["balance"])
            max_balance = max(max_balance, balance)

    def on_error(w, e):
        log.error(f"WS ERROR: {e}")

    def on_close(w, *_):
        log.warning("❌ WebSocket closed")

    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever()

# ================= START =================
if __name__ == "__main__":
    log.info("🔥 MOMENTO BOT STARTING")
    start_ws()