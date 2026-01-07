import os, json, time, threading, websocket
import numpy as np
from collections import deque
from sklearn.neural_network import MLPClassifier

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))

SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 100.0
MARTI_MULTIPLIER = 1.6
MAX_MARTI_STEPS = 4
SAFE_RISK = 0.05

EMA_FAST, EMA_MED, EMA_SLOW = 3, 6, 12
MIN_CONFIDENCE = 0.65
PROPOSAL_COOLDOWN = 1.0

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= STATE =================
ticks = deque(maxlen=500)
trade_queue = deque(maxlen=20)
bias_memory = deque(maxlen=200)

BALANCE = 0.0
WINS = LOSSES = TRADES = 0

ema_fast = ema_med = ema_slow = 0.0
current_stake = BASE_STAKE
marti_step = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0

ws = None
lock = threading.Lock()

# ================= UTIL =================
def log(msg):
    with lock:
        print(msg, flush=True)

def calc_ema(prev, price, p):
    alpha = 2 / (p + 1)
    return price if prev == 0 else prev * (1 - alpha) + price * alpha

# ================= MARKET LOGIC =================
def detect_trend():
    if ema_fast > ema_med > ema_slow:
        return "UP"
    if ema_fast < ema_med < ema_slow:
        return "DOWN"
    return None

def detect_momentum():
    if len(ticks) < 5:
        return None, 0
    arr = np.array(list(ticks)[-5:])
    delta = arr[-1] - arr[0]
    strength = min(abs(delta) / (np.std(arr) + 1e-6), 1)
    return ("UP" if delta > 0 else "DOWN"), strength

# ================= ML ENGINE =================
mlp = MLPClassifier(hidden_layer_sizes=(16,16), max_iter=300)
mlp_X = deque(maxlen=300)
mlp_y = deque(maxlen=300)
mlp_ready = False

def extract_features():
    if len(ticks) < 30:
        return None
    arr = np.array(list(ticks)[-30:])
    return np.array([
        arr[-1] - arr[-5],
        np.std(arr),
        np.mean(np.diff(arr)),
        ema_fast - ema_slow,
        np.max(arr) - np.min(arr)
    ])

def ml_predict():
    if not mlp_ready:
        return None, 0
    X = extract_features()
    if X is None:
        return None, 0
    p = mlp.predict_proba([X])[0]
    return ("UP" if p[1] > p[0] else "DOWN"), max(p)

def train_ml(direction, profit):
    global mlp_ready
    X = extract_features()
    if X is None:
        return
    y = 1 if direction == "UP" else 0
    if profit < 0:
        y = 1 - y
    mlp_X.append(X)
    mlp_y.append(y)
    if len(mlp_y) >= 20:
        mlp.fit(list(mlp_X), list(mlp_y))
        mlp_ready = True

# ================= STAKE CONTROL =================
def calculate_stake(conf):
    global marti_step
    stake = BASE_STAKE * (MARTI_MULTIPLIER ** marti_step) * (1 + conf)
    max_allowed = max(BALANCE * SAFE_RISK, BASE_STAKE)
    return min(stake, max_allowed, MAX_STAKE)

# ================= TRADE ENGINE =================
def evaluate_trade():
    global last_proposal_time
    now = time.time()

    if trade_in_progress:
        return
    if now - last_proposal_time < PROPOSAL_COOLDOWN:
        return

    trend = detect_trend()
    momentum, m_conf = detect_momentum()
    ml_dir, ml_conf = ml_predict()

    votes = {"UP": 0, "DOWN": 0}
    if trend: votes[trend] += 0.4
    if momentum: votes[momentum] += m_conf
    if ml_dir: votes[ml_dir] += ml_conf * 1.2

    direction = "UP" if votes["UP"] > votes["DOWN"] else "DOWN"
    confidence = abs(votes["UP"] - votes["DOWN"])

    if confidence < MIN_CONFIDENCE:
        return

    stake = calculate_stake(confidence)
    trade_queue.append((direction, stake))
    last_proposal_time = now
    log(f"🚀 QUEUED {direction} | Stake {stake:.2f} | Conf {confidence:.2f}")
    process_queue()

def process_queue():
    if trade_queue and not trade_in_progress:
        d, s = trade_queue.popleft()
        send_trade(d, s)

def send_trade(direction, stake):
    global trade_in_progress, last_trade_time
    trade_in_progress = True
    last_trade_time = time.time()

    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": "CALL" if direction == "UP" else "PUT",
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))
    log(f"📨 PROPOSAL SENT → {direction} | {stake:.2f}")

# ================= SETTLEMENT =================
def settle(contract):
    global BALANCE, WINS, LOSSES, TRADES, marti_step, trade_in_progress

    profit = float(contract.get("profit", 0))
    BALANCE += profit
    TRADES += 1

    direction = "UP" if contract["contract_type"] == "CALL" else "DOWN"
    train_ml(direction, profit)

    if profit > 0:
        WINS += 1
        marti_step = 0
    else:
        LOSSES += 1
        marti_step = min(marti_step + 1, MAX_MARTI_STEPS)

    trade_in_progress = False
    log(f"✔ SETTLED {profit:.2f} | BAL {BALANCE:.2f}")
    process_queue()

# ================= WS =================
def on_message(ws, msg):
    global BALANCE
    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("✔ AUTHORIZED")

    if "tick" in data:
        ticks.append(float(data["tick"]["quote"]))
        evaluate_trade()

    if "balance" in data:
        BALANCE = float(data["balance"]["balance"])

    if "proposal" in data:
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": data["proposal"]["amount"]}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold") or c.get("is_expired"):
            settle(c)

def on_open(ws):
    log("🔌 CONNECTED")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_close(ws, *_):
    log("❌ DISCONNECTED → RECONNECTING")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= AUTO UNFREEZE =================
def watchdog():
    global trade_in_progress
    while True:
        time.sleep(1)
        if trade_in_progress and time.time() - last_trade_time > 6:
            trade_in_progress = False
            log("⚠ AUTO-UNFREEZE")
            process_queue()

# ================= EMA ENGINE =================
def ema_engine():
    global ema_fast, ema_med, ema_slow
    while True:
        if ticks:
            p = ticks[-1]
            ema_fast = calc_ema(ema_fast, p, EMA_FAST)
            ema_med = calc_ema(ema_med, p, EMA_MED)
            ema_slow = calc_ema(ema_slow, p, EMA_SLOW)
        time.sleep(0.05)

# ================= MAIN =================
if __name__ == "__main__":
    log("🚀 MOMENTO CLOUD BOT STARTED")
    start_ws()
    threading.Thread(target=ema_engine, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    while True:
        time.sleep(60)