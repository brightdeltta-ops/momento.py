import os, json, time, threading
import websocket
from collections import deque

# ================= CONFIG =================
TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))

SYMBOL = "R_75"

BASE_STAKE = 200.0
TRADE_DURATION = 1  # ticks

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_LOOKBACK = 5

MAX_SESSION_LOSS = 5050.0
FREEZE_TIMEOUT = 5

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= STATE =================
ticks = deque(maxlen=500)
trade_queue = deque(maxlen=10)

BALANCE = 0.0
WINS = LOSSES = TRADES = 0

trade_open = False
last_trade_time = 0
last_proposal_time = 0

ws = None

# ================= LOG =================
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

# ================= INDICATORS =================
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    val = data[-period]
    for p in data[-period+1:]:
        val = p * k + val * (1 - k)
    return val

def micro_fractal():
    if len(ticks) < FRACTAL_LOOKBACK + 2:
        return None

    recent = list(ticks)[-FRACTAL_LOOKBACK-2:-2]
    hi = max(recent)
    lo = min(recent)
    last = ticks[-1]

    if last > hi:
        return "up"
    if last < lo:
        return "down"
    return None

def trade_signal():
    if len(ticks) < EMA_SLOW + 5:
        return None

    ef = ema(ticks, EMA_FAST)
    es = ema(ticks, EMA_SLOW)
    fractal = micro_fractal()
    price = ticks[-1]

    if not fractal or ef is None or es is None:
        return None

    if fractal == "up" and ef > es and price > ef:
        return "up"
    if fractal == "down" and ef < es and price < ef:
        return "down"

    return None

# ================= SAFETY =================
def session_loss_ok():
    return LOSSES * BASE_STAKE < MAX_SESSION_LOSS

def auto_unfreeze():
    global trade_open
    while True:
        time.sleep(2)
        if trade_open and time.time() - last_trade_time > FREEZE_TIMEOUT:
            trade_open = False
            log("⚠ Auto-unfreeze triggered")

# ================= EXECUTION =================
def evaluate():
    global last_proposal_time

    if trade_open:
        return
    if not session_loss_ok():
        return
    if time.time() - last_proposal_time < 0.4:
        return

    direction = trade_signal()
    if not direction:
        return

    trade_queue.append(direction)
    last_proposal_time = time.time()
    log(f"🎯 FRACTAL+EMA → {direction.upper()}")
    process_queue()

def process_queue():
    global trade_open, last_trade_time
    if trade_queue and not trade_open:
        direction = trade_queue.popleft()
        ct = "CALL" if direction == "up" else "PUT"

        ws.send(json.dumps({
            "proposal": 1,
            "amount": BASE_STAKE,
            "basis": "stake",
            "contract_type": ct,
            "currency": "USD",
            "duration": TRADE_DURATION,
            "duration_unit": "t",
            "symbol": SYMBOL
        }))

        trade_open = True
        last_trade_time = time.time()
        log(f"📨 Proposal {ct}")

# ================= WS HANDLERS =================
def on_open(ws):
    ws.send(json.dumps({"authorize": TOKEN}))

def on_message(ws, msg):
    global BALANCE, trade_open, WINS, LOSSES, TRADES

    data = json.loads(msg)

    if "authorize" in data:
        log("✅ Authorized")
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

    if "tick" in data:
        ticks.append(float(data["tick"]["quote"]))
        evaluate()

    if "proposal" in data:
        ws.send(json.dumps({
            "buy": data["proposal"]["id"],
            "price": BASE_STAKE
        }))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold") or c.get("is_expired"):
            profit = float(c.get("profit") or 0)
            BALANCE += profit
            TRADES += 1
            trade_open = False

            if profit > 0:
                WINS += 1
            else:
                LOSSES += 1

            log(f"✔ Settled | P/L {profit:.2f} | Bal {BALANCE:.2f}")
            process_queue()

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

def on_error(ws, err):
    log(f"❌ WS Error {err}")

def on_close(ws, *_):
    log("❌ WS Closed — reconnecting")
    time.sleep(2)
    start_ws()

# ================= START =================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

log("🚀 FRACTAL EMA BOT — KOYEB READY")
start_ws()
threading.Thread(target=auto_unfreeze, daemon=True).start()

while True:
    time.sleep(1)