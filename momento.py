import json, threading, time, websocket, numpy as np
from collections import deque
import os

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Set in Koyeb environment variables
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = "R_75"

BASE_STAKE = 250.0
TRADE_DURATION = 1  # ticks

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_LOOKBACK = 5

# ================= STATE =================
tick_history = deque(maxlen=500)
trade_queue = deque(maxlen=10)
active_trades = []

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0

authorized = False
ws_running = False
ws = None
lock = threading.Lock()

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOGGING =================
def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

# ================= UTILS =================
def auto_unfreeze():
    global trade_in_progress
    while True:
        time.sleep(2)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            log("⚠ Auto-unfreeze triggered")

def session_loss_check():
    return LOSSES * BASE_STAKE < 50

def calculate_ema(prices, period):
    arr = np.array(prices[-period:])
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.dot(arr, weights)

def micro_fractal():
    if len(tick_history) < FRACTAL_LOOKBACK + 2:
        return None
    recent = list(tick_history)[-FRACTAL_LOOKBACK-2:-2]
    high = max(recent)
    low = min(recent)
    current = tick_history[-1]
    if current > high:
        return "up"
    if current < low:
        return "down"
    return None

def trade_signal():
    if len(tick_history) < EMA_SLOW + 5:
        return None
    prices = list(tick_history)
    price = prices[-1]
    ema_fast = calculate_ema(prices, EMA_FAST)
    ema_slow = calculate_ema(prices, EMA_SLOW)
    fractal = micro_fractal()
    if not fractal:
        return None
    if fractal == "up" and ema_fast > ema_slow and price > ema_fast:
        return "up"
    if fractal == "down" and ema_fast < ema_slow and price < ema_fast:
        return "down"
    return None

def evaluate_and_trade():
    global last_proposal_time
    now = time.time()
    if trade_in_progress or now - last_proposal_time < 0.4:
        return
    if not session_loss_check():
        return
    direction = trade_signal()
    if not direction:
        return
    trade_queue.append((direction, TRADE_DURATION, BASE_STAKE))
    last_proposal_time = now
    log(f"🎯 FRACTAL + EMA → {direction.upper()} | Stake {BASE_STAKE}")
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        request_proposal(trade_queue.popleft())

def request_proposal(signal):
    global trade_in_progress, last_trade_time
    direction, duration, stake = signal
    ct = "CALL" if direction == "up" else "PUT"
    proposal = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": duration,
        "duration_unit": "t",
        "symbol": SYMBOL
    }
    trade_in_progress = True
    last_trade_time = time.time()
    ws.send(json.dumps(proposal))
    log(f"📨 Proposal → {direction.upper()}")

def on_contract_settlement(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    TRADE_COUNT += 1
    trade_in_progress = False
    if profit > 0:
        WINS += 1
        log(f"✔ WIN +{profit:.2f} | BAL {BALANCE:.2f}")
    else:
        LOSSES += 1
        log(f"❌ LOSS {profit:.2f} | BAL {BALANCE:.2f}")
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws, msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized = True
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
            log("✔ Authorized")
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            evaluate_and_trade()
        if "proposal" in data:
            pid = data["proposal"]["id"]
            time.sleep(0.05)
            ws.send(json.dumps({"buy": pid, "price": BASE_STAKE}))
        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
        if "balance" in data:
            BALANCE = float(data["balance"]["balance"])
    except Exception as e:
        log(f"⚠ WS Error: {e}")

def on_open(ws):
    log("🌐 Connected, authorizing…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, err):
    log(f"❌ WS Error: {err}")

def on_close(ws, *_):
    log("🔌 WS Closed — reconnecting in 3s")
    time.sleep(3)
    start_ws()

def start_ws():
    global ws, ws_running
    if ws_running:
        return
    ws_running = True
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

if __name__ == "__main__":
    log("🚀 FRACTAL EMA BOT — KOYEB CLOUD READY")
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    while True:
        time.sleep(1)