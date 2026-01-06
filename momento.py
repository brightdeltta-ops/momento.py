import json, threading, time, websocket, numpy as np, os
from collections import deque
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))
SYMBOL = os.getenv("SYMBOL", "R_75")

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

# Trading Parameters
BASE_STAKE = 1.0
MAX_STAKE = 20.0
TRADE_RISK_FRAC = 0.02
PROPOSAL_COOLDOWN = 1        # aggressive
PROPOSAL_DELAY = 0.5         # aggressive
EMA_FAST = 2
EMA_SLOW = 6
EMA_PULLBACK_FAST = 8
EMA_PULLBACK_SLOW = 50
MICRO_SLICE = 6
CONFIDENCE_THRESHOLD = 0.25

# Volatility adaptive
VOL_LOOKBACK = 12

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=30)
active_trades = []

BALANCE = 1000.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
authorized = False
ws_running = False
ws = None
lock = threading.Lock()

alerts = deque(maxlen=50)
equity_curve = []

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOGGING =================
def log(msg):
    with lock:
        alerts.appendleft(f"[{datetime.utcnow().isoformat()}] {msg}")
        print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1.,0.,period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def adaptive_stake(confidence=1.0):
    if len(tick_history) < VOL_LOOKBACK:
        return BASE_STAKE
    recent = np.array(list(tick_history)[-VOL_LOOKBACK:])
    vol = recent.std()
    stake = BASE_STAKE * (1 + vol*100) * confidence
    stake = max(BASE_STAKE, min(stake, MAX_STAKE))
    return stake

def session_loss_check():
    return LOSSES*BASE_STAKE < 50

def get_breakout_confidence():
    if len(tick_buffer) < 5:
        return None, 0
    recent = np.array(tick_buffer)
    high, low = recent.max(), recent.min()
    rng = high - low
    std = recent.std()
    if rng < 0.001:
        return None, 0
    current = recent[-1]
    if current >= high - 0.2*rng:
        return "up", min(1, std*200)
    if current <= low + 0.2*rng:
        return "down", min(1, std*200)
    momentum = current - recent[-3]
    return ("up" if momentum>0 else "down", min(1, abs(momentum)*150))

def ema_pullback_signal(prices):
    if len(prices) < EMA_PULLBACK_SLOW:
        return None
    data = np.array(prices)
    ema_fast = data[-EMA_PULLBACK_FAST:].mean()
    ema_slow = data[-EMA_PULLBACK_SLOW:].mean()
    last = data[-1]
    prev = data[-2]
    if last > ema_slow and prev < ema_fast and last > ema_fast:
        return "up"
    elif last < ema_slow and prev > ema_fast and last < ema_fast:
        return "down"
    return None

# ================= TRADE LOGIC =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        return
    direction, confidence = get_breakout_confidence()
    if direction is None or confidence < CONFIDENCE_THRESHOLD:
        return
    short_ema = calculate_ema(list(tick_buffer), EMA_FAST)
    long_ema = calculate_ema(list(tick_buffer), EMA_SLOW)
    if short_ema is None or long_ema is None:
        return
    ema_trend = "up" if short_ema > long_ema else "down"
    if direction != ema_trend:
        return
    pullback = ema_pullback_signal(list(tick_history))
    if pullback:
        direction = pullback
    TRADE_AMOUNT = adaptive_stake(confidence)
    trade_queue.append((direction,1,TRADE_AMOUNT))
    last_proposal_time = time.time()
    process_trade_queue()
    log(f"🚀 Queued {direction.upper()} | Stake {TRADE_AMOUNT:.2f} | Conf {confidence:.2f}")

def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        sig = trade_queue.popleft()
        request_proposal(sig)

def request_proposal(sig):
    global TRADE_AMOUNT, trade_in_progress, last_trade_time
    direction,duration,stake = sig
    TRADE_AMOUNT = stake
    ct = "CALL" if direction=="up" else "PUT"
    proposal = {
        "proposal":1,
        "amount":TRADE_AMOUNT,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":duration,
        "duration_unit":"t",
        "symbol":SYMBOL
    }
    trade_in_progress = True
    last_trade_time = time.time()
    ws.send(json.dumps(proposal))
    log(f"📨 Proposal sent → {direction.upper()} | Stake {TRADE_AMOUNT:.2f}")

def on_contract_settlement(c):
    global BALANCE,WINS,LOSSES,TRADE_COUNT,trade_in_progress
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    if profit > 0: WINS += 1
    else: LOSSES += 1
    TRADE_COUNT += 1
    trade_in_progress = False
    log(f"✔ Settlement → Profit: {profit:.2f} | Next Stake: {BASE_STAKE:.2f}")
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws,msg):
    global BALANCE,authorized
    try:
        data=json.loads(msg)
        if "authorize" in data:
            authorized=True
            resubscribe_channels()
            log("✔ Authorized")
        if "tick" in data:
            tick=float(data["tick"]["quote"])
            tick_history.append(tick)
            tick_buffer.append(tick)
            evaluate_and_trade()
        if "proposal" in data:
            pid=data["proposal"]["id"]
            time.sleep(PROPOSAL_DELAY)
            ws.send(json.dumps({"buy":pid,"price":TRADE_AMOUNT}))
            active_trades.append({"id":pid,"profit":0})
        if "proposal_open_contract" in data:
            c=data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
                active_trades[:] = [t for t in active_trades if t["id"]!=c.get("id")]
        if "balance" in data:
            BALANCE=float(data["balance"]["balance"])
            equity_curve.append(BALANCE)
    except Exception as e:
        log(f"⚠ WS Error: {e}")

def resubscribe_channels():
    if not authorized: return
    ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
    ws.send(json.dumps({"balance":1,"subscribe":1}))
    ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
    log("🔄 Resubscribed to all channels")

def on_error(ws,error):
    log(f"❌ WS Error: {error}")

def on_close(ws,code,msg):
    log("❌ WS Closed — reconnecting in 2s")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws, ws_running, authorized
    if ws_running: return
    ws_running=True
    authorized=False
    ws=websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda ws: ws.send(json.dumps({"authorize":API_TOKEN})),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever,daemon=True).start()
    log("🌐 WebSocket started")

def keep_alive():
    while True:
        time.sleep(15)
        try: ws.send(json.dumps({"ping":1}))
        except: pass

# ================= START =================
def start():
    log("🚀 EMA BREAKOUT + PULLBACK BOT — KOYEB READY (ENV COMPATIBLE)")
    start_ws()
    threading.Thread(target=keep_alive,daemon=True).start()
    while True:
        time.sleep(5)

if __name__=="__main__":
    start()