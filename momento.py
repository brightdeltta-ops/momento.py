import json, threading, time, websocket, numpy as np, os
from collections import deque

# ================= USER CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Must be set in Koyeb env
APP_ID = int(os.getenv("APP_ID", 112380))
SYMBOL = os.getenv("SYMBOL", "R_75")

BASE_STAKE = float(os.getenv("BASE_STAKE", 1.0))
MAX_STAKE = float(os.getenv("MAX_STAKE", 100.0))
TRADE_RISK_FRAC = float(os.getenv("TRADE_RISK_FRAC", 0.02))

EMA_FAST = 3
EMA_SLOW = 10

MICRO_SLICE = 10
VOLATILITY_WINDOW = 20
VOLATILITY_THRESHOLD = 0.0015

PROPOSAL_COOLDOWN = 6
PROPOSAL_DELAY = 12
MAX_DD = 0.20

PERSIST_FILE = "bot_state.json"

# ================= STATE =================
tick_history = deque(maxlen=500)
tick_buffer = deque(maxlen=MICRO_SLICE)
trade_queue = deque(maxlen=10)

BALANCE = 0.0
MAX_BALANCE = 0.0
WINS = LOSSES = TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

CURRENT_REGIME = None
trade_in_progress = False
last_proposal_time = 0
last_direction = None
authorized = False
ws_running = False
ws = None
lock = threading.Lock()

REGIME_STATS = {"trend": {"wins":0,"losses":0},"compression":{"wins":0,"losses":0}}
LOSS_STREAKS = {"trend":0,"compression":0}
REGIME_COOLDOWN_MULT = {"trend":1.0,"compression":1.0}

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= PERSISTENCE =================
def save_state():
    state = {
        "REGIME_STATS": REGIME_STATS,
        "LOSS_STREAKS": LOSS_STREAKS,
        "REGIME_COOLDOWN_MULT": REGIME_COOLDOWN_MULT,
        "BALANCE": BALANCE,
        "MAX_BALANCE": MAX_BALANCE,
        "WINS": WINS,
        "LOSSES": LOSSES,
        "TRADE_COUNT": TRADE_COUNT,
        "last_direction": last_direction
    }
    try:
        with open(PERSIST_FILE,"w") as f:
            json.dump(state,f)
    except Exception as e:
        print(f"⚠ Save state error: {e}")

def load_state():
    global REGIME_STATS, LOSS_STREAKS, REGIME_COOLDOWN_MULT
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT, last_direction
    if not os.path.exists(PERSIST_FILE):
        print("Fresh start — no saved state")
        return
    try:
        with open(PERSIST_FILE,"r") as f:
            content = f.read().strip()
            if not content: return
            state = json.loads(content)
        REGIME_STATS.update(state.get("REGIME_STATS", {}))
        LOSS_STREAKS.update(state.get("LOSS_STREAKS", {}))
        REGIME_COOLDOWN_MULT.update(state.get("REGIME_COOLDOWN_MULT", {}))
        BALANCE = state.get("BALANCE", BALANCE)
        MAX_BALANCE = state.get("MAX_BALANCE", MAX_BALANCE)
        WINS = state.get("WINS", WINS)
        LOSSES = state.get("LOSSES", LOSSES)
        TRADE_COUNT = state.get("TRADE_COUNT", TRADE_COUNT)
        last_direction = state.get("last_direction", last_direction)
        print("Persistent state loaded")
    except Exception as e:
        print(f"⚠ Failed to load state ({e}) — starting fresh")

# ================= UTILITIES =================
def calculate_ema(data, period):
    if len(data) < period: return None
    w = np.exp(np.linspace(-1.,0.,period))
    w /= w.sum()
    return np.convolve(data[-period:], w, mode="valid")[0]

def session_loss_check():
    if MAX_BALANCE == 0: return True
    return (MAX_BALANCE - BALANCE)/MAX_BALANCE < MAX_DD

def calculate_dynamic_stake(conf):
    return min(BASE_STAKE + conf * BALANCE * TRADE_RISK_FRAC, MAX_STAKE)

def check_trade_memory(direction):
    global last_direction
    if direction == last_direction:
        return False
    last_direction = direction
    return True

def regime_edge_multiplier(regime):
    stats = REGIME_STATS[regime]
    total = stats["wins"] + stats["losses"]
    if total < 10: return 1.0
    wr = stats["wins"]/total
    return 0.6 if wr<0.45 else 0.8 if wr<0.55 else 1.0 if wr<0.65 else 1.15 if wr<0.75 else 1.3

def adaptive_cooldown(regime):
    return PROPOSAL_COOLDOWN*REGIME_COOLDOWN_MULT.get(regime,1.0)

# ================= REGIME DETECTION =================
def detect_market_regime():
    if len(tick_buffer)<EMA_SLOW or len(tick_history)<VOLATILITY_WINDOW: return "idle",0.0
    prices = np.array(tick_buffer)
    vol = prices.std()
    ef = calculate_ema(prices, EMA_FAST)
    es = calculate_ema(prices, EMA_SLOW)
    if ef is None or es is None: return "idle",0.0
    diff = abs(ef-es)
    if vol>=VOLATILITY_THRESHOLD and diff>vol*0.3: return "trend", min(1.0,diff/vol)
    if vol<VOLATILITY_THRESHOLD*0.7: return "compression", min(1.0,(VOLATILITY_THRESHOLD-vol)/VOLATILITY_THRESHOLD)
    return "idle",0.0

# ================= STRATEGIES =================
def momentum_strategy(conf):
    ef = calculate_ema(list(tick_buffer), EMA_FAST)
    es = calculate_ema(list(tick_buffer), EMA_SLOW)
    if ef>es: return "up", conf
    if ef<es: return "down", conf
    return None,0

def compression_breakout_strategy(conf):
    r = np.array(tick_buffer)
    hi,lo = r.max(), r.min()
    rng = hi-lo
    if rng<0.0015: return None,0
    if r[-1]>=hi-0.15*rng: return "up",conf
    if r[-1]<=lo+0.15*rng: return "down",conf
    return None,0

# ================= MASTER LOGIC =================
def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, CURRENT_REGIME
    regime, base_conf = detect_market_regime()
    if regime=="idle" or base_conf<0.45: return
    if time.time()-last_proposal_time<adaptive_cooldown(regime): return
    if not session_loss_check():
        print("Drawdown limit hit")
        return
    conf = base_conf*regime_edge_multiplier(regime)
    conf *= max(0.6,1-0.15*LOSS_STREAKS[regime])
    if conf<0.5: return
    if regime=="trend": direction,conf=momentum_strategy(conf)
    else: direction,conf=compression_breakout_strategy(conf)
    if direction is None or not check_trade_memory(direction): return
    TRADE_AMOUNT = calculate_dynamic_stake(conf)
    trade_queue.append((direction,1,TRADE_AMOUNT))
    last_proposal_time = time.time()
    CURRENT_REGIME = regime
    print(f"{regime.upper()} {direction.upper()} | Conf {conf:.2f}")
    process_trade_queue()

# ================= TRADE FLOW =================
def process_trade_queue():
    if trade_queue and not trade_in_progress:
        request_proposal(trade_queue.popleft())

def request_proposal(sig):
    global trade_in_progress
    direction,duration,stake = sig
    ct = "CALL" if direction=="up" else "PUT"
    ws.send(json.dumps({
        "proposal":1,"amount":stake,"basis":"stake",
        "contract_type":ct,"currency":"USD",
        "duration":duration,"duration_unit":"t","symbol":SYMBOL
    }))
    trade_in_progress = True

def on_contract_settlement(c):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT
    global trade_in_progress, CURRENT_REGIME
    profit = float(c.get("profit") or 0)
    BALANCE+=profit
    MAX_BALANCE=max(MAX_BALANCE,BALANCE)
    if profit>0:
        WINS+=1
        REGIME_STATS[CURRENT_REGIME]["wins"]+=1
        LOSS_STREAKS[CURRENT_REGIME]=0
        REGIME_COOLDOWN_MULT[CURRENT_REGIME]=max(1.0,REGIME_COOLDOWN_MULT[CURRENT_REGIME]*0.85)
    else:
        LOSSES+=1
        REGIME_STATS[CURRENT_REGIME]["losses"]+=1
        LOSS_STREAKS[CURRENT_REGIME]+=1
        if LOSS_STREAKS[CURRENT_REGIME]>=2:
            REGIME_COOLDOWN_MULT[CURRENT_REGIME]*=1.5
        if LOSS_STREAKS[CURRENT_REGIME]>=4:
            REGIME_COOLDOWN_MULT[CURRENT_REGIME]=4.0
            print(f"{CURRENT_REGIME.upper()} FROZEN")
    TRADE_COUNT+=1
    trade_in_progress=False
    CURRENT_REGIME=None
    save_state()
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws,msg):
    global BALANCE
    data=json.loads(msg)
    if "authorize" in data:
        ws.send(json.dumps({"ticks":SYMBOL,"subscribe":1}))
        ws.send(json.dumps({"balance":1,"subscribe":1}))
        ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
        print("Authorized")
    if "tick" in data:
        tick=float(data["tick"]["quote"])
        tick_history.append(tick)
        tick_buffer.append(tick)
        evaluate_and_trade()
    if "proposal" in data:
        time.sleep(PROPOSAL_DELAY)
        ws.send(json.dumps({"buy":data["proposal"]["id"],"price":TRADE_AMOUNT}))
    if "proposal_open_contract" in data:
        c=data["proposal_open_contract"]
        if c.get("is_sold"): on_contract_settlement(c)
    if "balance" in data:
        BALANCE=float(data["balance"]["balance"])

def start_ws():
    global ws, ws_running
    if ws_running: return
    ws_running=True
    ws=websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda ws: ws.send(json.dumps({"authorize":API_TOKEN})),
        on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= START =================
def start():
    load_state()
    start_ws()
    print("🚀 Headless Bot — Koyeb Ready")
    while True:
        time.sleep(60)  # keep instance alive

if __name__=="__main__":
    start()