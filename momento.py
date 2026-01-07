# =====================================
# MOMENTO — QUANT ALPHA BOT (KOYEB READY)
# =====================================

import os, json, time, threading, websocket, numpy as np, pandas as pd
from collections import deque
import csv

# ================= ENV =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))
SYMBOL = os.getenv("SYMBOL", "R_75")
BASE_STAKE = float(os.getenv("BASE_STAKE", "50"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "100"))
TRADE_RISK_FRAC = 0.02

# ================= PARAMETERS =================
EMA_FAST_BASE = 6
EMA_SLOW_BASE = 13
EMA_CONF = 30
MIN_TICKS = 15
PROPOSAL_COOLDOWN = 32
GRADE_THRESHOLD_BASE = 0.02
TREND_MIN = 0.003
MIN_TRADE_INTERVAL = 64
LOW_CONF_WAIT = 30

TRADE_LOG = "trade_log.csv"

# ================= STATE =================
tick_history = deque(maxlen=300)
equity_curve = []
alerts = deque(maxlen=50)
trade_queue = deque(maxlen=50)
active_trades = []

BALANCE = 1000.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
TRADE_AMOUNT = BASE_STAKE

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0
next_trade_signal = None

authorized = False
ws_running = False
ws = None
lock = threading.Lock()
DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= UTILS =================
def append_alert(msg):
    with lock:
        alerts.appendleft(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def safe_ws_send(msg):
    global ws
    try:
        if ws: ws.send(json.dumps(msg))
    except Exception as e:
        append_alert(f"⚠ WS send error: {e}")

def log_trade(timestamp, direction, stake, grade, profit):
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG,"w",newline="") as f:
            csv.writer(f).writerow(["timestamp","direction","stake","grade","profit"])
    with open(TRADE_LOG,"a",newline="") as f:
        csv.writer(f).writerow([timestamp,direction,stake,grade,profit])

def ema(series,length):
    return series.ewm(span=length).mean()

# ================= QUANT LOGIC =================
def quant_distribution_factor(window=30):
    if len(tick_history)<window: return 1.0
    recent = np.array(list(tick_history)[-window:])
    mean,std = recent.mean(), recent.std()
    z = (recent[-1]-mean)/(std+1e-8)
    return np.clip(1+np.tanh(z),0.5,2.0)

def quant_trend_strength(window=15):
    if len(tick_history)<window: return 0.0
    recent = np.array(list(tick_history)[-window:])
    x = np.arange(len(recent))
    slope = np.polyfit(x,recent,1)[0]
    std = np.std(recent)
    return slope/(std+1e-8)

def numeric_trade_grade():
    if len(tick_history)<MIN_TICKS: return 0.0
    df = pd.DataFrame({"c": list(tick_history)})
    df["ema_f"] = ema(df["c"], EMA_FAST_BASE)
    df["ema_s"] = ema(df["c"], EMA_SLOW_BASE)
    df["ema_c"] = ema(df["c"], EMA_CONF)

    trend_score = quant_trend_strength()
    ema_score = (df["ema_f"].iloc[-1]-df["ema_s"].iloc[-1])/(df["c"].mean()+1e-8)
    quant_factor = quant_distribution_factor()
    momentum = (df["c"].iloc[-1]-df["c"].iloc[-2])/(df["c"].iloc[-2]+1e-8)

    grade = trend_score*0.45 + ema_score*0.3 + (quant_factor-1)*0.15 + momentum*0.1

    recent_vol = np.std(df["c"].iloc[-20:])
    dynamic_threshold = GRADE_THRESHOLD_BASE*(1+recent_vol*2)

    long_trend = df["ema_c"].iloc[-1]-df["ema_c"].iloc[-5]
    if (grade>0 and long_trend<0) or (grade<0 and long_trend>0): return 0.0
    if abs(trend_score)<TREND_MIN: return 0.0
    z = (df["c"].iloc[-1]-df["c"].mean())/(df["c"].std()+1e-8)
    if abs(z)>2.0: return 0.0
    return grade if abs(grade)>dynamic_threshold else 0.0

def calculate_trade_amount(grade):
    if len(tick_history)<10: return BASE_STAKE
    recent = np.array(list(tick_history)[-20:])
    vol = np.std(recent)/np.mean(recent)
    stake = BASE_STAKE*(0.05/max(vol,0.01))
    stake = min(stake, MAX_STAKE, BALANCE*TRADE_RISK_FRAC)
    stake *= quant_distribution_factor()
    stake *= min(abs(grade)*3,3.0)
    return max(stake, BASE_STAKE)

def evaluate_and_trade():
    global last_proposal_time, TRADE_AMOUNT, last_trade_time
    now = time.time()
    if now-last_proposal_time<PROPOSAL_COOLDOWN: return
    if now-last_trade_time<MIN_TRADE_INTERVAL: return
    if len(tick_history)<MIN_TICKS: return

    grade = numeric_trade_grade()
    direction = "RISE" if grade>0 else "FALL"

    if grade==0 and now-last_trade_time>LOW_CONF_WAIT:
        grade = 0.01
        TRADE_AMOUNT = BASE_STAKE
        append_alert("⚡ Low-confidence fire mode triggered!")

    if grade!=0:
        TRADE_AMOUNT = calculate_trade_amount(grade)
        TRADE_AMOUNT = max(TRADE_AMOUNT, BASE_STAKE)
        trade_queue.append((direction, grade, TRADE_AMOUNT))
        last_proposal_time = now
        process_trade_queue()
    else:
        append_alert("⚠ Skipping weak trade grade")

def process_trade_queue():
    global next_trade_signal, trade_in_progress
    if trade_queue and not trade_in_progress:
        direction, grade, stake = trade_queue.popleft()
        next_trade_signal = direction
        request_proposal(next_trade_signal, grade, stake)

def request_proposal(direction, grade, stake):
    global trade_in_progress, TRADE_AMOUNT, last_trade_time
    ct = "CALL" if direction=="RISE" else "PUT"
    proposal = {
        "proposal":1,
        "amount":stake,
        "basis":"stake",
        "contract_type":ct,
        "currency":"USD",
        "duration":2,
        "duration_unit":"t",
        "symbol":SYMBOL
    }
    trade_in_progress=True
    last_trade_time=time.time()
    append_alert(f"📨 Proposal → {direction} | Stake {stake:.2f} | Grade {grade:.4f}")
    safe_ws_send(proposal)

def on_contract_settlement(contract):
    global BALANCE, WINS, LOSSES, TRADE_AMOUNT, TRADE_COUNT, trade_in_progress
    profit = float(contract.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    if profit>0: WINS+=1
    else: LOSSES+=1
    TRADE_COUNT+=1
    append_alert(f"✔ Settlement: {profit:.2f} | Next Stake {TRADE_AMOUNT:.2f}")
    log_trade(time.strftime("%Y-%m-%d %H:%M:%S"), next_trade_signal, TRADE_AMOUNT, 0, profit)
    trade_in_progress=False
    process_trade_queue()

# ================= WEBSOCKET =================
def on_message(ws_obj, msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)
        if "authorize" in data:
            authorized=True
            safe_ws_send({"ticks":SYMBOL,"subscribe":1})
            safe_ws_send({"balance":1,"subscribe":1})
            safe_ws_send({"proposal_open_contract":1,"subscribe":1})
            append_alert("✔ Authorized")
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            evaluate_and_trade()
        if "balance" in data:
            BALANCE=float(data["balance"]["balance"])
            equity_curve.append(BALANCE)
        if "proposal" in data:
            pid = data["proposal"]["id"]
            time.sleep(0.01)
            safe_ws_send({"buy":pid,"price":TRADE_AMOUNT})
            active_trades.append(pid)
            append_alert("✔ BUY sent")
        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)
                active_trades[:] = [t for t in active_trades if t != c.get("id")]
    except Exception as e:
        append_alert(f"⚠ WS Handler Error: {e}")

def start_ws():
    global ws, ws_running
    if ws_running: return
    ws_running=True
    def run_ws():
        global ws
        while True:
            try:
                ws = websocket.WebSocketApp(
                    DERIV_WS,
                    on_open=lambda ws_obj: safe_ws_send({"authorize":API_TOKEN}),
                    on_message=on_message,
                    on_error=lambda ws_obj, err: append_alert(f"❌ WS Error: {err}"),
                    on_close=lambda *args: append_alert("❌ WS Closed")
                )
                ws.run_forever()
            except Exception as e:
                append_alert(f"⚠ WS reconnect error: {e}")
                time.sleep(2)
    threading.Thread(target=run_ws, daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        safe_ws_send({"ping":1})

def auto_unfreeze():
    global trade_in_progress, last_trade_time
    while True:
        time.sleep(1)
        if trade_in_progress and time.time()-last_trade_time>5:
            trade_in_progress=False
            append_alert("⚠ Auto-unfreeze: Trade reset")
            process_trade_queue()

def heartbeat():
    while True:
        append_alert(f"❤️ BAL={BALANCE:.2f} W={WINS} L={LOSSES} OPEN={len(active_trades)}")
        time.sleep(30)

# ================= START =================
def start():
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    while True:
        time.sleep(1)

if __name__=="__main__":
    append_alert("🚀 QUANT ALPHA BOT — KOYEB READY")
    start()