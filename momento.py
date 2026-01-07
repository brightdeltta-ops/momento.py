import os
import json
import time
import threading
import websocket
import numpy as np
from collections import deque

# ================= USER CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Token from Koyeb env variable
if not API_TOKEN:
    raise ValueError("API token not set. Please set DERIV_API_TOKEN in Koyeb environment variables.")

APP_ID = 112380
SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
TRADE_RISK_FRAC = 0.02
PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 1  # seconds delay before buying proposal
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOLATILITY_WINDOW = 20
VOLATILITY_THRESHOLD = 0.0015
MAX_DD = 0.2  # 20% drawdown

# ================= DATA TRACKING =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)
active_trades = []

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
last_direction = None
authorized = False
ws_running = False
ws = None
lock = threading.Lock()
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= UTILITIES =================
def append_alert(msg):
    with lock:
        print(msg)

def auto_unfreeze():
    global trade_in_progress, last_trade_time
    while True:
        time.sleep(2)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            append_alert("⚠ Auto-unfreeze triggered")

def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1.,0.,period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode="valid")[0]

def is_market_expanding():
    if len(tick_history) < VOLATILITY_WINDOW:
        return False
    window = np.array(list(tick_history)[-VOLATILITY_WINDOW:])
    vol = window.std()
    return vol >= VOLATILITY_THRESHOLD

def session_loss_check():
    global BALANCE
    max_loss = BALANCE * MAX_DD
    return LOSSES * BASE_STAKE < max_loss

def calculate_dynamic_stake(confidence):
    global BALANCE
    base_risk = BALANCE * TRADE_RISK_FRAC
    stake = BASE_STAKE + confidence * base_risk
    return min(stake, MAX_STAKE)

def check_trade_memory(direction):
    global last_direction
    if direction == last_direction:
        return False
    last_direction = direction
    return True

# ================= TRADE LOGIC =================
def get_breakout_confidence():
    if len(tick_buffer) < 5:
        return None, 0
    recent = np.array(tick_buffer)
    high, low = recent.max(), recent.min()
    rng = high - low
    std = recent.std()
    if rng < 0.002:
        return None, 0
    current = recent[-1]
    if current >= high - 0.2 * rng:
        return "up", min(1, std*200)
    if current <= low + 0.2 * rng:
        return "down", min(1, std*200)
    momentum = current - recent[-3]
    return ("up" if momentum>0 else "down", min(1, abs(momentum)*150))

def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        append_alert("⚠ Session drawdown exceeded. Pausing trades.")
        return
    if not is_market_expanding():
        append_alert("⚠ Market not in expansion. Trade skipped.")
        return
    direction, confidence = get_breakout_confidence()
    if confidence < 0.5:
        append_alert(f"⚠ Skipping low-confidence trade ({confidence:.2f})")
        return
    if not check_trade_memory(direction):
        append_alert("⚠ Trade blocked by memory filter")
        return
    short_ema = calculate_ema(list(tick_buffer), EMA_FAST)
    long_ema = calculate_ema(list(tick_buffer), EMA_SLOW)
    if short_ema is None or long_ema is None:
        return
    ema_trend = "up" if short_ema > long_ema else "down"
    if direction != ema_trend:
        append_alert(f"⚠ EMA ({ema_trend}) and breakout ({direction}) mismatch")
        return
    if len(tick_buffer) >= 3:
        micro_trend = tick_buffer[-1] - tick_buffer[-3]
        if (direction=="up" and micro_trend<=0) or (direction=="down" and micro_trend>=0):
            append_alert("⚠ Skipping micro-countertrend trade")
            return
    TRADE_AMOUNT = calculate_dynamic_stake(confidence)
    trade_queue.append((direction, 1, TRADE_AMOUNT))
    last_proposal_time = time.time()
    append_alert(f"🚀 Queued {direction.upper()} | Stake {TRADE_AMOUNT:.2f} | Conf {confidence:.2f}")
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        sig = trade_queue.popleft()
        request_proposal(sig)

def request_proposal(sig):
    global TRADE_AMOUNT, trade_in_progress, last_trade_time
    direction, duration, stake = sig
    TRADE_AMOUNT = stake
    ct = "CALL" if direction=="up" else "PUT"
    proposal = {
        "proposal": 1,
        "amount": TRADE_AMOUNT,
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
    append_alert(f"📨 Proposal sent → {direction.upper()} | Stake {TRADE_AMOUNT:.2f}")

def on_contract_settlement(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress, MAX_BALANCE
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    if profit > 0:
        WINS += 1
    else:
        LOSSES += 1
    TRADE_COUNT += 1
    trade_in_progress = False
    append_alert(f"✔ Settlement → Profit: {profit:.2f} | Drawdown: {(MAX_BALANCE-BALANCE)/MAX_BALANCE*100 if MAX_BALANCE>0 else 0:.1f}%")
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws, msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized = True
            resubscribe_channels()
            append_alert("✔ Authorized")
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            evaluate_and_trade()
        if "proposal" in data:
            pid = data["proposal"]["id"]
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy": pid, "price": TRADE_AMOUNT}))
            active_trades.append({"id": pid, "profit": 0})
        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
                active_trades[:] = [t for t in active_trades if t["id"] != c.get("id")]
        if "balance" in data:
            BALANCE = float(data["balance"]["balance"])
    except Exception as e:
        append_alert(f"⚠ WS Error: {e}")

def resubscribe_channels():
    if not authorized:
        return
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
    append_alert("🔄 Resubscribed to all channels")

def on_error(ws, error):
    append_alert(f"❌ WS Error: {error}")

def on_close(ws, code, msg):
    append_alert("❌ WS Closed — reconnecting in 2s")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws, ws_running, authorized
    if ws_running: return
    ws_running = True
    authorized = False
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda ws: ws.send(json.dumps({"authorize": API_TOKEN})),
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

# ================= START =================
def start():
    append_alert("🚀 MOMENTO BOT — HEADLESS KOYEB READY")
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    while True:
        time.sleep(1)  # Keep script alive

if __name__ == "__main__":
    start()