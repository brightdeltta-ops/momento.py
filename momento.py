import json, threading, time, websocket, os, numpy as np
from collections import deque
from datetime import datetime

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # set in Koyeb environment
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = "R_75"

# ================= STRATEGY CONFIG =================
BASE_STAKE = 100.0
MAX_STAKE = 1000.0
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 2
TRADE_TIMEOUT = 12
VOLATILITY_WINDOW = 20
VOLATILITY_THRESHOLD = 0.0015
MAX_DD = 0.2  # 20% drawdown
PROFIT_LOCK = 50.0

# ================= STATE =================
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

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOG =================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ================= EMA =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

# ================= MARKET FILTERS =================
def is_market_expanding():
    if len(tick_history) < VOLATILITY_WINDOW:
        return False
    window = np.array(list(tick_history)[-VOLATILITY_WINDOW:])
    return window.std() >= VOLATILITY_THRESHOLD

def session_loss_check():
    global BALANCE
    max_loss = BALANCE * MAX_DD
    return LOSSES*BASE_STAKE < max_loss and BALANCE >= -MAX_DD*100

def check_trade_memory(direction):
    global last_direction
    if direction == last_direction:
        return False
    last_direction = direction
    return True

def calculate_dynamic_stake(confidence):
    global BALANCE
    base_risk = BALANCE * 0.02
    stake = BASE_STAKE + confidence * base_risk
    return min(stake, MAX_STAKE)

# ================= MICRO-FRACTAL + EMA =================
def get_breakout_confidence():
    if len(tick_buffer) < 5:
        return None,0
    recent = np.array(tick_buffer)
    high, low = recent.max(), recent.min()
    rng = high - low
    std = recent.std()
    if rng < 0.002:
        return None,0
    current = recent[-1]
    if current >= high - 0.2*rng:
        return "up", min(1,std*200)
    if current <= low + 0.2*rng:
        return "down", min(1,std*200)
    momentum = current - recent[-3]
    return ("up" if momentum>0 else "down", min(1,abs(momentum)*150))

def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT
    if trade_in_progress or time.time() - last_proposal_time < PROPOSAL_COOLDOWN:
        return
    if not session_loss_check():
        log("⚠ Session drawdown exceeded. Pausing trades.")
        return
    if not is_market_expanding():
        log("⚠ Market not in expansion. Trade skipped.")
        return
    direction, confidence = get_breakout_confidence()
    if confidence < 0.5 or not check_trade_memory(direction):
        return

    # EMA alignment
    short_ema = calculate_ema(list(tick_buffer), EMA_FAST)
    long_ema = calculate_ema(list(tick_buffer), EMA_SLOW)
    if short_ema is None or long_ema is None:
        return
    ema_trend = "up" if short_ema > long_ema else "down"
    if direction != ema_trend:
        return

    # Micro-momentum
    if len(tick_buffer)>=3:
        micro_trend = tick_buffer[-1]-tick_buffer[-3]
        if (direction=="up" and micro_trend<=0) or (direction=="down" and micro_trend>=0):
            return

    TRADE_AMOUNT = calculate_dynamic_stake(confidence)
    trade_queue.append((direction,1,TRADE_AMOUNT))
    last_proposal_time = time.time()
    log(f"🚀 Queued {direction.upper()} | Stake {TRADE_AMOUNT:.2f} | Conf {confidence:.2f}")
    process_trade_queue()

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
    global BALANCE,WINS,LOSSES,TRADE_COUNT,trade_in_progress,MAX_BALANCE
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE,BALANCE)
    if profit>0: WINS+=1
    else: LOSSES+=1
    TRADE_COUNT+=1
    trade_in_progress=False
    log(f"✔ Settlement → Profit: {profit:.2f} | Drawdown: {(MAX_BALANCE-BALANCE)/MAX_BALANCE*100:.1f}%")
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws,msg):
    global BALANCE,authorized
    try:
        data=json.loads(msg)
        if "authorize" in data:
            authorized=True
            ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
            ws.send(json.dumps({"balance":1,"subscribe":1}))
            ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
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
    except Exception as e:
        log(f"⚠ WS Error: {e}")

def on_error(ws,error): log(f"❌ WS Error: {error}")
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

def keep_alive():
    while True:
        time.sleep(15)
        try: ws.send(json.dumps({"ping":1}))
        except: pass

def watchdog():
    global trade_in_progress
    while True:
        time.sleep(3)
        if trade_in_progress and time.time()-last_trade_time>12:
            trade_in_progress=False
            log("⚠ TRADE TIMEOUT RESET")

# ================= START =================
if __name__=="__main__":
    threading.Thread(target=keep_alive,daemon=True).start()
    threading.Thread(target=watchdog,daemon=True).start()
    start_ws()