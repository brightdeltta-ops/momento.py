# =====================================
# MOMENTO — MULTI-CONTRACT ALPHA BOT (KOYEB READY)
# =====================================

import os, json, time, threading, websocket
from collections import deque
import numpy as np

# ================= ENV =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = os.getenv("SYMBOL", "R_75")
BASE_STAKE = float(os.getenv("BASE_STAKE", "1"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "100"))

# ================= PARAMETERS =================
EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10
VOLATILITY_THRESHOLD = 0.0015
PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 5
MAX_DD = 0.20
MULTI_CONTRACTS = 2
REGIME_CONFIRM_TICKS = 5

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)
OPEN_CONTRACTS = {}

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = LOSSES = TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

CURRENT_REGIME = None
last_proposal_time = 0
trade_in_progress = False
last_direction = None

regime_buffer = deque(maxlen=REGIME_CONFIRM_TICKS)
LOSS_STREAKS = {"trend":0,"compression":0}

alerts = deque(maxlen=40)
json_logs = deque(maxlen=60)
equity_curve = []

lock = threading.Lock()
ws = None
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= UTILITIES =================
def log(msg):
    with lock:
        alerts.appendleft(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def calculate_ema(data, period):
    if len(data) < period:
        return None
    w = np.exp(np.linspace(-1.,0.,period))
    w /= w.sum()
    return np.convolve(list(data)[-period:], w, mode="valid")[0]

def momentum_slope():
    if len(tick_buffer) < 5:
        return 0
    y = np.array(list(tick_buffer)[-5:])
    x = np.arange(len(y))
    return np.polyfit(x, y, 1)[0]

def calculate_dynamic_stake(conf):
    return max(BASE_STAKE, min(BASE_STAKE + conf * BALANCE * 0.02, MAX_STAKE))

def session_loss_check():
    if MAX_BALANCE==0:
        return True
    return (MAX_BALANCE - BALANCE)/MAX_BALANCE < MAX_DD

def regime_penalty(regime):
    return 1 + LOSS_STREAKS.get(regime,0)*0.7

# ================= REGIME DETECTION =================
def detect_market_regime():
    if len(tick_buffer)<EMA_SLOW or len(tick_history)<MICRO_SLICE:
        return "idle", 0.0

    prices = np.array(list(tick_buffer))
    vol = prices.std()
    ef = calculate_ema(prices, EMA_FAST)
    es = calculate_ema(prices, EMA_SLOW)

    if ef is None or es is None:
        return "idle", 0.0

    diff = abs(ef-es)

    if vol>=VOLATILITY_THRESHOLD and diff>vol*0.3:
        regime, conf = "trend", min(1.0,diff/vol)
    elif vol<VOLATILITY_THRESHOLD*0.7:
        regime, conf = "compression", min(1.0,(VOLATILITY_THRESHOLD-vol)/VOLATILITY_THRESHOLD)
    else:
        regime, conf = "idle",0.0

    regime_buffer.append(regime)
    if len(regime_buffer)==REGIME_CONFIRM_TICKS and len(set(regime_buffer))==1:
        return regime, conf
    return "idle",0.0

# ================= STRATEGIES =================
def momentum_strategy(conf):
    ef = calculate_ema(list(tick_buffer), EMA_FAST)
    es = calculate_ema(list(tick_buffer), EMA_SLOW)
    slope = momentum_slope()
    if ef>es and slope>0: return "up", conf
    if ef<es and slope<0: return "down", conf
    return None, 0

def compression_breakout_strategy(conf):
    r = np.array(list(tick_buffer))
    hi, lo = r.max(), r.min()
    rng = hi-lo
    if rng<0.0015: return None,0
    if r[-1]>=hi-0.15*rng: return "up", conf
    if r[-1]<=lo+0.15*rng: return "down", conf
    return None,0

# ================= TRADE ENGINE =================
def evaluate_and_trade():
    global last_proposal_time, CURRENT_REGIME, TRADE_AMOUNT

    regime, conf = detect_market_regime()
    if regime=="idle" or conf<0.5:
        return

    cooldown = PROPOSAL_COOLDOWN*regime_penalty(regime)
    if time.time()-last_proposal_time<cooldown:
        return

    if not session_loss_check():
        log("⚠ MAX DRAWDOWN HIT")
        return

    if regime=="trend":
        direction, conf = momentum_strategy(conf)
    else:
        direction, conf = compression_breakout_strategy(conf)

    if direction is None:
        return

    TRADE_AMOUNT = calculate_dynamic_stake(conf)
    split = TRADE_AMOUNT/MULTI_CONTRACTS

    for _ in range(MULTI_CONTRACTS):
        trade_queue.append((direction,1,split))

    CURRENT_REGIME = regime
    last_proposal_time=time.time()
    log(f"{regime.upper()} → {direction.upper()} | Stake {TRADE_AMOUNT:.2f}")
    process_trade_queue()

def process_trade_queue():
    global trade_in_progress
    while trade_queue and not trade_in_progress:
        direction,duration,stake=trade_queue.popleft()
        ct="CALL" if direction=="up" else "PUT"
        ws.send(json.dumps({
            "proposal":1,
            "amount":stake,
            "basis":"stake",
            "contract_type":ct,
            "currency":"USD",
            "duration":duration,
            "duration_unit":"t",
            "symbol":SYMBOL
        }))
        trade_in_progress=True
        log(f"🚀 Trade sent: {ct} | Stake: {stake}")

# ================= CONTRACT MANAGEMENT =================
def manage_contract(c):
    cid=c["contract_id"]
    if cid not in OPEN_CONTRACTS:
        OPEN_CONTRACTS[cid]={"entry":c["buy_price"],"peak":0,"partial":False}

    trade=OPEN_CONTRACTS[cid]
    profit=float(c.get("profit") or 0)
    stake=float(c["buy_price"])
    pnl=profit/stake
    trade["peak"]=max(trade["peak"], pnl)

    if pnl<=-0.35:
        ws.send(json.dumps({"sell":cid,"price":0}))
        log("❌ VIRTUAL SL")
    if pnl>=0.25 and not trade["partial"]:
        ws.send(json.dumps({"sell":cid,"price":c["bid_price"]}))
        trade["partial"]=True
        log("✅ PARTIAL TP")
    if trade["peak"]>=0.3 and pnl<=trade["peak"]-0.15:
        ws.send(json.dumps({"sell":cid,"price":0}))
        log("🔒 TRAIL STOP")

def on_contract_settlement(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, trade_in_progress
    profit=float(c.get("profit") or 0)
    BALANCE+=profit
    MAX_BALANCE=max(MAX_BALANCE,BALANCE)
    if profit>0:
        WINS+=1
        LOSS_STREAKS[CURRENT_REGIME]=0
    else:
        LOSSES+=1
        LOSS_STREAKS[CURRENT_REGIME]+=1
    OPEN_CONTRACTS.pop(c["contract_id"],None)
    trade_in_progress=False
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(wsapp,msg):
    json_logs.appendleft(msg[:160])
    data=json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
        ws.send(json.dumps({"balance":1,"subscribe":1}))
        ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
        log("🔑 AUTHORIZED")

    if "tick" in data:
        price=float(data["tick"]["quote"])
        tick_history.append(price)
        tick_buffer.append(price)
        evaluate_and_trade()

    if "proposal" in data:
        time.sleep(PROPOSAL_DELAY)
        ws.send(json.dumps({"buy":data["proposal"]["id"],"price":data["proposal"]["ask_price"]}))

    if "proposal_open_contract" in data:
        c=data["proposal_open_contract"]
        if not c.get("is_sold"):
            manage_contract(c)
        else:
            on_contract_settlement(c)

    if "balance" in data:
        global BALANCE
        BALANCE=float(data["balance"]["balance"])

def on_open(wsapp):
    log("CONNECTED & AUTHORIZED")
    wsapp.send(json.dumps({"authorize":API_TOKEN}))

def on_error(wsapp,error):
    log(f"WS ERROR: {error}")

def on_close(wsapp, close_status_code, close_msg):
    log(f"WS CLOSED | code={close_status_code} msg={close_msg}")
    reconnect_ws()

def reconnect_ws():
    log("🔄 Attempting WS reconnect in 5s...")
    time.sleep(5)
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

# ================= HEARTBEAT =================
def heartbeat():
    while True:
        log(f"❤️ BAL={BALANCE:.2f} W={WINS} L={LOSSES} OPEN={len(OPEN_CONTRACTS)}")
        time.sleep(30)

# ================= START =================
if __name__=="__main__":
    log("🚀 MULTI-CONTRACT ALPHA BOT — KOYEB READY")
    threading.Thread(target=heartbeat, daemon=True).start()
    start_ws()
    while True:
        time.sleep(1)