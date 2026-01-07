import os, json, websocket, threading, time, numpy as np
from collections import deque

# =================================================
# USER CONFIG
# =================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # ← set this in Koyeb environment
APP_ID = int(os.getenv("APP_ID", "0"))   # ← set this in Koyeb environment
SYMBOL = "R_75"

BASE_STAKE = 1.0
PROPOSAL_COOLDOWN = 0.2
EMA_SHORT = 3
EMA_LONG = 10
CONFIDENCE_THRESHOLD = 0.6
MAX_LOSSES = 50
MAX_CONSECUTIVE_LOSSES = 3

ML_ENABLED = True
ML_WINDOW = 10
MICRO_PATTERN_WINDOW = 5

# =================================================
# DATA STRUCTURES
# =================================================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=20)
vol_buffer = deque(maxlen=20)
trade_queue = deque(maxlen=50)
active_trades = []
alerts = deque(maxlen=50)
equity_curve = []
pattern_history = deque(maxlen=100)

BALANCE = 10000.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
CONSECUTIVE_LOSSES = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
TRADE_AMOUNT = BASE_STAKE

authorized = False
ws_running = False
ws = None
lock = threading.Lock()
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# =================================================
# ONLINE ML
# =================================================
ML_FEATURES = ML_WINDOW
ML_WEIGHTS = None
ML_BIAS = 0.0
ML_LR = 0.05
ML_TRAINED = False

def sigmoid(x):
    return 1 / (1 + np.exp(-x))

def build_features():
    if len(tick_buffer) < ML_FEATURES + 1:
        return None
    seq = np.array(list(tick_buffer)[-ML_FEATURES:])
    diff = np.diff(seq)
    momentum = seq[-1] - seq[-3]
    vol = np.std(seq)
    ema_ratio = calculate_ema(seq, EMA_SHORT)/calculate_ema(seq, EMA_LONG)
    return np.array([*diff, momentum, vol, ema_ratio])

def ml_predict_next_tick():
    global ML_WEIGHTS, ML_BIAS, ML_TRAINED
    X = build_features()
    if X is None: return None, 0
    y = 1 if tick_buffer[-1] - tick_buffer[-2] > 0 else 0
    if ML_WEIGHTS is None: ML_WEIGHTS = np.zeros_like(X)
    z = np.dot(X, ML_WEIGHTS) + ML_BIAS
    pred_prob = sigmoid(z)
    pred = "up" if pred_prob > 0.5 else "down"
    error = y - pred_prob
    ML_WEIGHTS += ML_LR * error * X
    ML_BIAS += ML_LR * error
    ML_TRAINED = True
    conf = max(pred_prob, 1 - pred_prob)
    return pred, conf

# =================================================
# UTILITY FUNCTIONS
# =================================================
def append_alert(msg):
    with lock:
        alerts.appendleft(msg)
    print(msg)

def calculate_ema(data, period=5):
    arr = np.array(data[-period:])
    if len(arr) < period: return float(data[-1])
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    ema = np.convolve(arr, weights, mode='valid')
    return float(ema[-1])

def session_loss_check():
    return LOSSES < MAX_LOSSES and CONSECUTIVE_LOSSES < MAX_CONSECUTIVE_LOSSES

# =================================================
# MICRO-PREDICTION LOGIC
# =================================================
def get_micro_direction():
    if len(tick_buffer) < 10: return None
    diffs = [tick_buffer[i]-tick_buffer[i-1] for i in range(1,len(tick_buffer))]
    trend = sum(diffs[-5:])
    chop = sum(abs(x) for x in diffs[-8:])
    micro = diffs[-1]
    if abs(trend) > chop*0.35:
        return "up" if trend>0 else "down"
    if chop < 0.12:
        return "up" if micro>0 else "down"
    return "up" if micro>=0 else "down"

def breakout_prediction():
    if len(vol_buffer)<10: return None,0
    recent = list(vol_buffer)[-10:]
    highs, max_low = max(recent), min(recent)
    current = recent[-1]
    rng = highs - max_low
    std = np.std(recent)
    if rng < 0.002: return None,0
    if current >= highs - (rng*0.2): return "up", min(1,std*200)
    if current <= max_low + (rng*0.2): return "down", min(1,std*200)
    momentum = current - recent[-3]
    return ("up" if momentum>0 else "down", min(1,abs(momentum)*150))

def order_flow_signal():
    if len(vol_buffer) < 5: return 1.0
    recent = np.array(list(vol_buffer)[-5:])
    spike = recent[-1] - np.mean(recent[:-1])
    return min(max(spike*50,0),1)

def update_pattern_history():
    if len(tick_buffer) < MICRO_PATTERN_WINDOW: return
    pattern = tuple(np.sign([tick_buffer[i+1]-tick_buffer[i] for i in range(-MICRO_PATTERN_WINDOW,-1)]))
    pattern_history.append(pattern)

def micro_pattern_confidence():
    if len(tick_buffer) < MICRO_PATTERN_WINDOW: return 0
    current_pattern = tuple(np.sign([tick_buffer[i+1]-tick_buffer[i] for i in range(-MICRO_PATTERN_WINDOW,-1)]))
    matches = [p for p in pattern_history if p == current_pattern]
    return min(len(matches)/10,1)

# =================================================
# COMBINED CONFIDENCE
# =================================================
def combined_confidence():
    breakout_dir, breakout_conf = breakout_prediction()
    ema_trend = "up" if calculate_ema(list(tick_buffer), EMA_SHORT) > calculate_ema(list(tick_buffer), EMA_LONG) else "down"
    micro_dir = get_micro_direction()
    flow_conf = order_flow_signal()
    pattern_conf = micro_pattern_confidence()
    if breakout_dir != ema_trend or breakout_dir != micro_dir: return None,0
    conf = (breakout_conf*0.3 + flow_conf*0.25 + pattern_conf*0.2)
    final_dir = breakout_dir
    if ML_ENABLED:
        ml_dir, ml_conf = ml_predict_next_tick()
        if ml_dir != final_dir or ml_conf < 0.5: return None,0
        conf = conf*0.4 + ml_conf*0.6
    if pattern_conf < 0.2: return None,0
    return final_dir, conf

# =================================================
# TRADE LOGIC
# =================================================
def evaluate_and_trade():
    global last_proposal_time
    now = time.time()
    if now - last_proposal_time < PROPOSAL_COOLDOWN: return
    if not session_loss_check(): return
    signal, confidence = combined_confidence()
    if not signal or confidence < CONFIDENCE_THRESHOLD: return
    stake = BASE_STAKE * confidence
    trade_queue.append((signal,1,stake))
    last_proposal_time = now
    append_alert(f"📈 SIGNAL → {signal.upper()} | Stake {stake:.2f} | Conf {confidence:.2f}")
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        sig = trade_queue.popleft()
        request_proposal(sig)

def request_proposal(signal_tuple):
    global TRADE_AMOUNT, trade_in_progress, last_trade_time
    direction,duration,stake = signal_tuple
    TRADE_AMOUNT = stake
    ct = "CALL" if direction=="up" else "PUT"
    proposal={
        "proposal":1,
        "amount":TRADE_AMOUNT,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":duration,
        "duration_unit":"t",
        "symbol":SYMBOL
    }
    trade_in_progress=True
    last_trade_time=time.time()
    try: ws.send(json.dumps(proposal))
    except: append_alert("⚠ WS not ready for proposal")
    append_alert(f"💰 Proposal sent → {direction.upper()} | Stake {TRADE_AMOUNT:.2f}")

def on_contract_settlement(c):
    global BALANCE,WINS,LOSSES,TRADE_COUNT,trade_in_progress, CONSECUTIVE_LOSSES
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    append_alert(f"✔ Settlement → Profit: {profit:.2f} | Balance: {BALANCE:.2f}")
    if profit > 0:
        WINS += 1
        CONSECUTIVE_LOSSES = 0
    else:
        LOSSES += 1
        CONSECUTIVE_LOSSES += 1
    TRADE_COUNT += 1
    trade_in_progress=False
    process_trade_queue()

# =================================================
# WEBSOCKET
# =================================================
def on_message(ws,msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized=True
            ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
            ws.send(json.dumps({"balance":1,"subscribe":1}))
            ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
            append_alert("✔ Authorized")
        if "tick" in data:
            tick=float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            vol_buffer.append(tick)
            update_pattern_history()
            evaluate_and_trade()
        if "proposal" in data:
            pid=data["proposal"]["id"]
            try: ws.send(json.dumps({"buy":pid,"price":TRADE_AMOUNT}))
            except: append_alert("⚠ Buy proposal failed")
        if "proposal_open_contract" in data:
            c=data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
        if "balance" in data:
            BALANCE=float(data["balance"]["balance"])
    except Exception as e:
        append_alert(f"⚠ WS Error: {e}")

def on_error(ws, error): append_alert(f"❌ WS Error: {error}")
def on_close(ws, code, msg):
    append_alert("❌ WS Closed, reconnecting in 2s")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws, ws_running, authorized
    if ws_running: return
    ws_running=True
    authorized=False
    ws=websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=lambda ws: ws.send(json.dumps({"authorize":API_TOKEN})),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever,daemon=True).start()

# =================================================
# AUTO-UNFREEZE
# =================================================
def auto_unfreeze():
    global trade_in_progress, last_trade_time
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress=False
            append_alert("⚠ Auto-unfreeze triggered")

# =================================================
# START BOT
# =================================================
def start():
    start_ws()
    threading.Thread(target=auto_unfreeze,daemon=True).start()
    while True: time.sleep(1)

if __name__=="__main__":
    start()