# =====================================
# MULTI-CONTRACT ALPHA BOT — KOYEB READY
# =====================================

import os
import json
import time
import threading
import websocket
import numpy as np
from collections import deque

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Must set in Koyeb env variables
APP_ID = int(os.getenv("APP_ID", 112380))
SYMBOL = os.getenv("SYMBOL", "R_75")

BASE_STAKE = float(os.getenv("BASE_STAKE", 200))
MAX_STAKE = float(os.getenv("MAX_STAKE", 1000))
TRADE_RISK_FRAC = float(os.getenv("TRADE_RISK_FRAC", 0.02))

EMA_FAST_BASE = int(os.getenv("EMA_FAST_BASE", 6))
EMA_SLOW_BASE = int(os.getenv("EMA_SLOW_BASE", 13))
EMA_CONF = int(os.getenv("EMA_CONF", 30))

MIN_TICKS = int(os.getenv("MIN_TICKS", 15))
PROPOSAL_COOLDOWN = float(os.getenv("PROPOSAL_COOLDOWN", 32))
GRADE_THRESHOLD_BASE = float(os.getenv("GRADE_THRESHOLD_BASE", 0.02))
TREND_MIN = float(os.getenv("TREND_MIN", 0.003))
MIN_TRADE_INTERVAL = float(os.getenv("MIN_TRADE_INTERVAL", 64))
LOW_CONF_WAIT = float(os.getenv("LOW_CONF_WAIT", 30))

# ================= STATE =================
tick_history = deque(maxlen=300)
equity_curve = []
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

# ================= UTILITIES =================
def safe_ws_send(msg):
    global ws
    try:
        if ws is not None:
            ws.send(json.dumps(msg))
    except Exception as e:
        print(f"⚠ WS send error: {e}")

def ema(series, length):
    arr = np.array(series)
    if len(arr) < length:
        return arr[-1] if len(arr) else 0
    weights = np.exp(np.linspace(-1., 0., length))
    weights /= weights.sum()
    return np.convolve(arr[-length:], weights, mode='valid')[0]

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
    series = np.array(list(tick_history))
    ef = ema(series, EMA_FAST_BASE)
    es = ema(series, EMA_SLOW_BASE)
    ec = ema(series, EMA_CONF)

    trend_score = quant_trend_strength()
    ema_score = (ef - es) / (series.mean() + 1e-8)
    quant_factor = quant_distribution_factor()
    momentum = (series[-1] - series[-2]) / (series[-2] + 1e-8)

    grade = trend_score*0.45 + ema_score*0.3 + (quant_factor-1)*0.15 + momentum*0.1

    recent_vol = np.std(series[-20:])
    dynamic_threshold = GRADE_THRESHOLD_BASE * (1 + recent_vol*2)
    long_trend = ec - ema(series[-5:], EMA_CONF)
    if (grade > 0 and long_trend < 0) or (grade < 0 and long_trend > 0):
        return 0.0
    if abs(trend_score) < TREND_MIN:
        return 0.0
    z = (series[-1] - series.mean()) / (series.std() + 1e-8)
    if abs(z) > 2.0:
        return 0.0
    return grade if abs(grade) > dynamic_threshold else 0.0

def calculate_trade_amount(grade):
    recent = np.array(list(tick_history)[-20:])
    vol = np.std(recent)/np.mean(recent)
    stake = BASE_STAKE*(0.05/max(vol,0.01))
    stake = min(stake, MAX_STAKE, BALANCE*TRADE_RISK_FRAC)
    stake *= quant_distribution_factor()
    stake *= min(abs(grade)*3, 3.0)
    return max(stake, BASE_STAKE)

# ================= TRADE ENGINE =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_trade_time, trade_in_progress
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
        print("⚡ Low-confidence fire mode triggered!")

    if grade != 0:
        TRADE_AMOUNT = calculate_trade_amount(grade)
        TRADE_AMOUNT = max(TRADE_AMOUNT, BASE_STAKE)
        trade_queue.append((direction, grade, TRADE_AMOUNT))
        last_proposal_time = now
        process_trade_queue()

def process_trade_queue():
    global trade_in_progress, next_trade_signal
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
    print(f"📨 Proposal → {direction} | Stake {stake:.2f} | Grade {grade:.4f}")
    safe_ws_send(proposal)

def on_contract_settlement(contract):
    global BALANCE, WINS, LOSSES, TRADE_AMOUNT, TRADE_COUNT, trade_in_progress
    profit = float(contract.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    if profit > 0:
        WINS += 1
    else:
        LOSSES += 1
    TRADE_COUNT += 1
    trade_in_progress = False
    process_trade_queue()
    print(f"✔ Settlement: {profit:.2f} | Next Stake {TRADE_AMOUNT:.2f}")

# ================= WEBSOCKET =================
def on_message(ws_obj, msg):
    global BALANCE, authorized, active_trades
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized = True
            safe_ws_send({"ticks": SYMBOL, "subscribe":1})
            safe_ws_send({"balance":1, "subscribe":1})
            safe_ws_send({"proposal_open_contract":1, "subscribe":1})
            print("✔ Authorized")

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
            print("✔ BUY sent")

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
                active_trades[:] = [t for t in active_trades if t != c.get("id")]

    except Exception as e:
        print(f"⚠ WS Handler Error: {e}")

# ================= WS BOOT =================
def start_ws():
    global ws, ws_running
    if ws_running:
        return
    ws_running = True

    def run_ws():
        global ws
        while True:
            try:
                ws = websocket.WebSocketApp(
                    DERIV_WS,
                    on_open=lambda ws_obj: safe_ws_send({"authorize": API_TOKEN}),
                    on_message=on_message,
                    on_error=lambda ws_obj, err: print(f"❌ WS Error: {err}"),
                    on_close=lambda *args: print("❌ WS Closed")
                )
                ws.run_forever()
            except Exception as e:
                print(f"⚠ WS reconnect error: {e}")
                time.sleep(2)
    threading.Thread(target=run_ws, daemon=True).start()

def auto_unfreeze():
    global trade_in_progress
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            process_trade_queue()
            print("⚠ Auto-unfreeze: Trade reset")

def start():
    start_ws()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    print("🚀 Multi-Contract Alpha Bot — KOYEB READY")

if __name__ == "__main__":
    start()
    while True:
        time.sleep(60)  # keep instance alive