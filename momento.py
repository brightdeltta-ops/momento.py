import json, threading, time, websocket
from collections import deque
import numpy as np
import os
import sys

# ================================
# CONFIG
# ================================
API_TOKEN = os.getenv("DERIV_API_TOKEN", "eZjDrK54yMTAsbf")
APP_ID = int(os.getenv("APP_ID", 112380))
SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
MARTI_MULTIPLIER = 1.7
MAX_MARTI_STEPS = 5
SAFE_ACCOUNT_RISK = 0.05
MAX_SESSION_LOSS = 20

TRADE_AMOUNT = BASE_STAKE
EMA_FAST, EMA_MEDIUM, EMA_SLOW = 2, 4, 8
TIMEFRAMES = [1, 5, 15]
FRACTAL_LENGTH = 2
MICRO_TICKS = 3
PROPOSAL_COOLDOWN = 0.02  # ultra-HF

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================================
# STATE
# ================================
tick_history = deque(maxlen=1000)
alerts = deque(maxlen=50)
equity_curve = []

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADES = 0

trade_queue = deque(maxlen=20)
trade_memory = deque(maxlen=400)
bias_memory = deque(maxlen=400)

trade_in_progress = False
current_trade_amount = TRADE_AMOUNT
current_marti_step = 0
last_trade_time = 0
last_proposal_time = 0
next_trade_signal = None

ema_fast_val = 0
ema_medium_val = 0
ema_slow_val = 0

BAR_HISTORY = {tf: deque(maxlen=100) for tf in TIMEFRAMES}
ws = None
lock = threading.Lock()

tick_counter = 0
FAST_RETRAIN_INTERVAL = 10

# ================================
# UTILITIES
# ================================
def append_alert(msg):
    with lock:
        alerts.appendleft(msg)
    print(msg)

def calc_ema(prev, price, p):
    alpha = 2 / (p + 1)
    return price if prev == 0 else prev * (1 - alpha) + price * alpha

# ================================
# TREND DETECTION
# ================================
def aggregate_bars():
    if len(tick_history) < max(TIMEFRAMES): return
    now = time.time()
    for tf in TIMEFRAMES:
        last_bar = BAR_HISTORY[tf][-1][0] if BAR_HISTORY[tf] else None
        tf_ticks = list(tick_history)[-tf:]
        if tf_ticks:
            avg_price = sum(tf_ticks) / len(tf_ticks)
            if not last_bar or avg_price != last_bar:
                BAR_HISTORY[tf].append((avg_price, now))

def detect_fractal_tf(bar_deque):
    if len(bar_deque) < FRACTAL_LENGTH: return None
    for i in range(1, len(bar_deque) - 1):
        middle = bar_deque[i][0]
        left = bar_deque[i - 1][0]
        right = bar_deque[i + 1][0]
        if middle < min(left, right): return "RISE"
        elif middle > max(left, right): return "FALL"
    return None

def detect_breakout():
    if len(tick_history) < 5: return None
    recent = list(tick_history)[-5:]
    high, low = max(recent), min(recent)
    current = recent[-1]
    if current >= high: return "RISE"
    elif current <= low: return "FALL"
    return None

def detect_trend():
    global ema_fast_val, ema_medium_val, ema_slow_val
    if ema_fast_val > ema_medium_val > ema_slow_val: return "RISE"
    if ema_fast_val < ema_medium_val < ema_slow_val: return "FALL"
    return None

def detect_strength():
    if len(tick_history) < 20: return 0
    arr = np.array(list(tick_history)[-20:])
    d = arr[-1] - arr[0]
    s = np.std(arr)
    return min(abs(d) / (s if s != 0 else 1), 1)

# ================================
# SAFE MARTINGALE
# ================================
def calculate_safe_stake(fractal_strength):
    global current_marti_step, BALANCE
    base = BASE_STAKE * (1 + fractal_strength * 10)
    vol = np.std(list(tick_history)[-20:]) if len(tick_history) >= 20 else 0
    marti_multiplier = min(MARTI_MULTIPLIER, 1 + vol * 2)
    stake = base * (marti_multiplier ** current_marti_step)
    max_allowed = max(BALANCE * SAFE_ACCOUNT_RISK, BASE_STAKE)
    if stake > max_allowed:
        stake = BASE_STAKE
        current_marti_step = 0
    return min(stake, MAX_STAKE)

def update_marti_step(profit):
    global current_marti_step
    if profit > 0:
        current_marti_step = 0
    else:
        current_marti_step += 1
        if current_marti_step > MAX_MARTI_STEPS:
            current_marti_step = 0

# ================================
# ENGINE PREDICTION
# ================================
def ultra_engine_predict():
    direction, conf = None, 0
    trend = detect_trend()
    if trend: direction, conf = trend, 0.8
    breakout = detect_breakout()
    if breakout: direction, conf = breakout, 0.9
    return direction, conf

# ================================
# TRADE LOGIC
# ================================
def session_loss_check(): return LOSSES * BASE_STAKE < MAX_SESSION_LOSS

def evaluate_and_queue_trade():
    global last_proposal_time, tick_counter
    now = time.time()
    if now - last_proposal_time < PROPOSAL_COOLDOWN: return
    if not session_loss_check(): return

    direction, conf = ultra_engine_predict()
    if not direction: return

    fractal_strength = max(list(tick_history)[-FRACTAL_LENGTH:]) - min(list(tick_history)[-FRACTAL_LENGTH:])
    duration = 2
    if fractal_strength > 0.01: duration = 3
    if fractal_strength > 0.015: duration = 4

    stake = calculate_safe_stake(fractal_strength)
    trade_queue.append((direction, duration, stake))
    last_proposal_time = now
    append_alert(f"🚀 QUEUED {direction} | Stake {stake:.2f} | Conf {conf:.2f}")

    tick_counter += 1
    process_trade_queue()

def process_trade_queue():
    global next_trade_signal, trade_in_progress
    if trade_queue and not trade_in_progress:
        next_trade_signal = trade_queue.popleft()
        direction, duration, stake = next_trade_signal
        fire_trade(direction, duration, stake)

def fire_trade(direction, duration, stake):
    global trade_in_progress, current_trade_amount, last_trade_time
    if trade_in_progress: return
    trade_in_progress = True
    current_trade_amount = stake
    last_trade_time = time.time()
    contract = "CALL" if direction == "RISE" else "PUT"
    proposal = {
        "proposal": 1, "amount": stake, "basis": "stake",
        "contract_type": contract, "currency": "USD",
        "duration": duration, "duration_unit": "t",
        "symbol": SYMBOL
    }
    ws.send(json.dumps(proposal))
    append_alert(f"📨 PROPOSAL SENT → {direction} | {stake:.2f}")

def on_contract_settlement(contract):
    global BALANCE, WINS, LOSSES, TRADE_AMOUNT, current_marti_step, TRADES, trade_in_progress
    profit = float(contract.get("profit", 0))
    BALANCE += profit
    equity_curve.append(BALANCE)
    direction = "RISE" if contract.get("contract_type") == "CALL" else "FALL"
    if profit > 0: WINS += 1
    else: LOSSES += 1
    update_marti_step(profit)
    TRADE_AMOUNT = max(BASE_STAKE, TRADE_AMOUNT * 0.45) if profit > 0 else min(TRADE_AMOUNT * MARTI_MULTIPLIER, MAX_STAKE)
    TRADES += 1
    trade_in_progress = False
    append_alert(f"✔ SETTLED {profit:.2f} | BAL {BALANCE:.2f}")
    process_trade_queue()

# ================================
# WEBSOCKET
# ================================
def on_open(ws_conn):
    append_alert("✔ Connected → Authorizing")
    ws_conn.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws_conn, msg):
    global BALANCE, current_trade_amount
    data = json.loads(msg)
    if "authorize" in data:
        append_alert("✔ Authorized")
        ws_conn.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws_conn.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws_conn.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
    if "tick" in data:
        tick_history.append(float(data["tick"]["quote"]))
        evaluate_and_queue_trade()
    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])
        equity_curve.append(BALANCE)
    if "proposal" in data:
        ws_conn.send(json.dumps({"buy": data["proposal"]["id"], "price": current_trade_amount}))
        append_alert("💰 Contract Bought")
    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold") or c.get("is_expired"):
            on_contract_settlement(c)

def on_error(ws_conn, err): append_alert(f"❌ ERROR: {err}")

def on_close(ws_conn, *args):
    append_alert("❌ Closed → Reconnecting…")
    time.sleep(2)
    start_ws()

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

# ================================
# AUTO-UNFREEZE
# ================================
def auto_unfreeze():
    global trade_in_progress, last_trade_time
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            append_alert("⚠ AUTO-UNFREEZE triggered")
            process_trade_queue()

# ================================
# ENGINE LOOP
# ================================
def engine():
    global ema_fast_val, ema_medium_val, ema_slow_val
    while True:
        if len(tick_history) < EMA_SLOW:
            time.sleep(0.01)
            continue
        price = tick_history[-1]
        ema_fast_val = calc_ema(ema_fast_val, price, EMA_FAST)
        ema_medium_val = calc_ema(ema_medium_val, price, EMA_MEDIUM)
        ema_slow_val = calc_ema(ema_slow_val, price, EMA_SLOW)
        time.sleep(0.01)

# ================================
# MAIN
# ================================
if __name__ == "__main__":
    append_alert("🚀 Bot started — Koyeb-compatible")
    start_ws()
    threading.Thread(target=engine, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    # Keep script alive
    while True:
        time.sleep(10)