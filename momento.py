import websocket
import json
import threading
import time
import numpy as np
import os
from collections import deque
from datetime import datetime

# ============================================================
# CONFIG (Koyeb ENVIRONMENT VARIABLES)
# ============================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))
SYMBOL = os.getenv("SYMBOL", "R_75")

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

# Trading parameters
BASE_STAKE = 200.0
MAX_STAKE = 240.0
TRADE_RISK_FRAC = 0.02
EMA_FAST_BASE = 6
EMA_SLOW_BASE = 13
EMA_CONF = 30
MIN_TICKS = 15
PROPOSAL_COOLDOWN = 0.2
MIN_TRADE_INTERVAL = 4
LOW_CONF_WAIT = 30  # idle seconds before low-confidence trade

# ============================================================
# STATE
# ============================================================
tick_history = deque(maxlen=300)
equity_curve = []
alerts = deque(maxlen=50)
trade_queue = deque(maxlen=50)
active_trades = []

BALANCE = 1000.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
TRADE_AMOUNT = BASE_STAKE
next_trade_signal = None

authorized = False
ws_running = False
ws = None
lock = threading.Lock()
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ============================================================
# LOGGING / ALERTS
# ============================================================
def log(msg):
    with lock:
        alerts.appendleft(f"[{datetime.utcnow().isoformat()}] {msg}")
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

def safe_ws_send(msg):
    try:
        if ws is not None:
            ws.send(json.dumps(msg))
    except Exception as e:
        log(f"⚠ WS send error: {e}")

# ============================================================
# UTILITY FUNCTIONS
# ============================================================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    weights = np.exp(np.linspace(-1.,0.,period))
    weights /= weights.sum()
    return np.convolve(data[-period:], weights, mode='valid')[0]

def quant_distribution_factor(window=30):
    if len(tick_history) < window:
        return 1.0
    recent_ticks = np.array(list(tick_history)[-window:])
    mean = np.mean(recent_ticks)
    std = np.std(recent_ticks)
    current = recent_ticks[-1]
    z = (current - mean) / (std + 1e-8)
    return np.clip(1.0 + np.tanh(z), 0.5, 2.0)

def quant_trend_strength(window=15):
    if len(tick_history) < window:
        return 0.0
    recent = np.array(list(tick_history)[-window:])
    x = np.arange(len(recent))
    slope = np.polyfit(x, recent, 1)[0]
    std = np.std(recent)
    return slope / (std + 1e-8)

def numeric_trade_grade():
    if len(tick_history) < MIN_TICKS:
        return 0.0
    recent = np.array(list(tick_history)[-MIN_TICKS:])
    ema_f = calculate_ema(recent, EMA_FAST_BASE)
    ema_s = calculate_ema(recent, EMA_SLOW_BASE)
    ema_c = calculate_ema(recent, EMA_CONF)
    trend_score = quant_trend_strength()
    ema_score = (ema_f - ema_s) / (np.mean(recent)+1e-8)
    quant_factor = quant_distribution_factor()
    momentum = (recent[-1] - recent[-2]) / (recent[-2]+1e-8)
    grade = trend_score*0.45 + ema_score*0.3 + (quant_factor-1)*0.15 + momentum*0.1
    recent_vol = np.std(recent)
    dynamic_threshold = 0.02 * (1 + recent_vol*2)
    return grade if abs(grade) > dynamic_threshold else 0.0

def calculate_trade_amount(grade):
    recent = np.array(list(tick_history)[-20:])
    vol = np.std(recent)/np.mean(recent) if len(recent) >= 2 else 0.01
    stake = BASE_STAKE*(0.05/max(vol,0.01))
    stake = min(stake, MAX_STAKE, BALANCE*TRADE_RISK_FRAC)
    stake *= quant_distribution_factor()
    stake *= min(abs(grade)*3, 3.0)
    return max(stake, BASE_STAKE)

# ============================================================
# TRADE LOGIC
# ============================================================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_trade_time
    now = time.time()
    if now - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if now - last_trade_time < MIN_TRADE_INTERVAL:
        return
    if len(tick_history) < MIN_TICKS:
        return
    grade = numeric_trade_grade()
    direction = "RISE" if grade > 0 else "FALL"
    if grade == 0 and now - last_trade_time > LOW_CONF_WAIT:
        grade = 0.01
        TRADE_AMOUNT = BASE_STAKE
        log("⚡ Low-confidence fire mode triggered!")
    if grade != 0:
        TRADE_AMOUNT = calculate_trade_amount(grade)
        trade_queue.append((direction, grade, TRADE_AMOUNT))
        last_proposal_time = now
        process_trade_queue()
    else:
        log("⚠ Skipping weak trade grade")

def process_trade_queue():
    global next_trade_signal
    if trade_queue and not trade_in_progress:
        direction, grade, stake = trade_queue.popleft()
        next_trade_signal = direction
        request_proposal(direction, grade, stake)

def request_proposal(direction, grade, stake):
    global trade_in_progress, TRADE_AMOUNT, last_trade_time
    ct = "CALL" if direction=="RISE" else "PUT"
    proposal = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 2,
        "duration_unit": "t",
        "symbol": SYMBOL
    }
    trade_in_progress = True
    last_trade_time = time.time()
    log(f"📨 Proposal → {direction} | Stake {stake:.2f} | Grade {grade:.4f}")
    safe_ws_send(proposal)

def on_contract_settlement(contract):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(contract.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    if profit > 0: WINS += 1
    else: LOSSES += 1
    TRADE_COUNT += 1
    trade_in_progress = False
    log(f"✔ Settlement: {profit:.2f} | Next Stake {TRADE_AMOUNT:.2f}")
    process_trade_queue()

# ============================================================
# WEBSOCKET HANDLER
# ============================================================
def on_message(ws_obj, msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized = True
            safe_ws_send({"ticks": SYMBOL, "subscribe": 1})
            safe_ws_send({"balance": 1, "subscribe": 1})
            safe_ws_send({"proposal_open_contract": 1, "subscribe": 1})
            log("✔ Authorized")
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            evaluate_and_trade()
        if "balance" in data:
            BALANCE = float(data["balance"]["balance"])
            equity_curve.append(BALANCE)
        if "proposal" in data:
            pid = data["proposal"]["id"]
            time.sleep(0.01)
            safe_ws_send({"buy": pid, "price": TRADE_AMOUNT})
            active_trades.append(pid)
            log("✔ BUY sent")
        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
                active_trades[:] = [t for t in active_trades if t != c.get("id")]
    except Exception as e:
        log(f"⚠ WS Handler Error: {e}")

# ============================================================
# WEBSOCKET BOOT
# ============================================================
def start_ws():
    global ws, ws_running
    if ws_running: return
    ws_running = True

    def run_ws():
        global ws
        while True:
            try:
                ws = websocket.WebSocketApp(
                    DERIV_WS,
                    on_open=lambda ws_obj: safe_ws_send({"authorize": API_TOKEN}),
                    on_message=on_message,
                    on_error=lambda ws_obj, err: log(f"❌ WS Error: {err}"),
                    on_close=lambda *args: log("❌ WS Closed")
                )
                ws.run_forever()
            except Exception as e:
                log(f"⚠ WS reconnect error: {e}")
                time.sleep(2)
    threading.Thread(target=run_ws, daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        safe_ws_send({"ping": 1})

def auto_unfreeze():
    global trade_in_progress, last_trade_time
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            log("⚠ Auto-unfreeze: Trade reset")
            process_trade_queue()

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat():
    while True:
        time.sleep(30)
        log("❤️ HEARTBEAT")

# ============================================================
# START BOT
# ============================================================
def start():
    log("🚀 FIRE FIXED BOT — KOYEB READY (ENV COMPATIBLE)")
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(5)

if __name__ == "__main__":
    start()