import json, threading, time, websocket, numpy as np
from collections import deque
import os

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Must set in Koyeb env
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = "R_75"

BASE_STAKE = 200
TRADE_AMOUNT = BASE_STAKE
MAX_STAKE = 1000
MIN_TICKS = 15
VOL_THRESHOLD = 0.03
PROPOSAL_COOLDOWN = 1.1
PROPOSAL_DELAY = 0.05

EMA_F = 6
EMA_S = 13

# ================= STATE =================
tick_history = deque(maxlen=300)
trade_queue = deque(maxlen=10)
equity_curve = []

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
next_trade_signal = None

ws = None
lock = threading.Lock()

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOGGING =================
def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

# ================= UTILS =================
def ema(prices, length):
    if len(prices) < length:
        return None
    w = np.exp(np.linspace(-1., 0., length))
    w /= w.sum()
    return np.convolve(prices[-length:], w, mode='valid')[-1]

def auto_unfreeze():
    global trade_in_progress
    while True:
        time.sleep(2)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            log("⚠ Auto-unfreeze: Trade reset")

# ================= STRATEGY =================
def evaluate_and_trade():
    global last_proposal_time

    now = time.time()
    if now - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if len(tick_history) < MIN_TICKS:
        return

    prices = list(tick_history)
    micro_range = max(prices[-12:]) - min(prices[-12:])
    if micro_range < VOL_THRESHOLD:
        return

    fast = ema(prices, EMA_F)
    slow = ema(prices, EMA_S)
    if not fast or not slow:
        return

    direction = None
    if fast > slow and prices[-1] > prices[-2]:
        direction = "RISE"
    elif fast < slow and prices[-1] < prices[-2]:
        direction = "FALL"
    else:
        return

    trade_queue.append(direction)
    last_proposal_time = now
    process_trade_queue()

def process_trade_queue():
    global next_trade_signal
    if trade_queue and not trade_in_progress:
        next_trade_signal = trade_queue.popleft()
        request_proposal(next_trade_signal)

def request_proposal(direction):
    global trade_in_progress, last_trade_time
    ct = "CALL" if direction == "RISE" else "PUT"

    proposal = {
        "proposal": 1,
        "amount": TRADE_AMOUNT,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 2,
        "duration_unit": "t",
        "symbol": SYMBOL
    }

    trade_in_progress = True
    last_trade_time = time.time()
    log(f"📨 Proposal Request → {direction} | Stake {TRADE_AMOUNT}")
    ws.send(json.dumps(proposal))

def on_contract_settlement(contract):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(contract.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    TRADE_COUNT += 1
    trade_in_progress = False

    if profit > 0:
        WINS += 1
        log(f"✔ WIN +{profit:.2f} | BAL {BALANCE:.2f}")
    else:
        LOSSES += 1
        log(f"❌ LOSS {profit:.2f} | BAL {BALANCE:.2f}")

# ================= WS HANDLERS =================
def on_message(ws, msg):
    global BALANCE
    try:
        data = json.loads(msg)

        if data.get("msg_type") == "authorize":
            log("🔐 Authorized")
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

        if data.get("msg_type") == "tick":
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            evaluate_and_trade()

        if data.get("msg_type") == "balance":
            BALANCE = float(data["balance"]["balance"])
            equity_curve.append(BALANCE)

        if data.get("msg_type") == "proposal":
            pid = data["proposal"]["id"]
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy": pid, "price": TRADE_AMOUNT}))
            log(f"💸 BUY Sent → ID {pid}")

        if data.get("msg_type") == "proposal_open_contract":
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)

    except Exception as e:
        log(f"⚠ Handler Error: {e}")

def on_open(ws):
    log("🌐 Connected, authorizing…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, err):
    log(f"❌ WS ERROR: {err}")

def on_close(ws, *_):
    log("🔌 WS CLOSED — reconnecting in 3s")
    time.sleep(3)
    start_ws()

# ================= WS START =================
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

def keep_alive():
    while True:
        time.sleep(15)
        try:
            ws.send(json.dumps({"ping": 1}))
            log("❤️ HEARTBEAT")
        except:
            pass

# ================= MAIN =================
if __name__ == "__main__":
    log("🚀 EMA + MICRO BREAKOUT BOT — KOYEB CLOUD READY")
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()

    while True:
        time.sleep(1)