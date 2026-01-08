import os
import json
import time
import threading
import websocket
from collections import deque

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "108380"))

if not API_TOKEN:
    raise RuntimeError("DERIV_API_TOKEN not set")

SYMBOL = "R_10"   # V10 1s sniper
BASE_STAKE = 200.0
COOLDOWN_SECONDS = 6
BREAKOUT_FACTOR = 1.8
SNIPER_CONF = 0.92

# ================= STATE =================
tick_buffer = deque(maxlen=60)
price_buffer = deque(maxlen=100)
equity_curve = []
alerts = deque(maxlen=10)

BALANCE = 0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0
trade_in_progress = False
last_sniper_entry = 0

ws = None
lock = threading.Lock()

# ================= LOG =================
def alert(msg):
    alerts.appendleft(msg)
    print(time.strftime("%H:%M:%S"), msg, flush=True)

# ================= EMA & SNIPER LOGIC =================
def calc_ema(prices, period=20):
    if len(prices) < period:
        return None
    # Simple EMA without numpy
    k = 2 / (period + 1)
    ema_val = prices[-period]
    for p in prices[-period+1:]:
        ema_val = p*k + ema_val*(1-k)
    return ema_val

def smooth_tick(last, new):
    SMOOTHING_WEIGHT = 5
    return (last*(SMOOTHING_WEIGHT-1) + new)/SMOOTHING_WEIGHT

def calculate_sniper_signal():
    if len(tick_buffer) < 8 or len(price_buffer) < 20:
        return None

    ema_val = calc_ema(list(price_buffer))
    current_price = tick_buffer[-1]
    if ema_val is None:
        return None

    trend = "BULL" if current_price > ema_val else "BEAR"

    smoothed = [smooth_tick(tick_buffer[i-1], tick_buffer[i]) for i in range(1,len(tick_buffer))]
    recent = smoothed[-5:]
    momentum = recent[-1] - recent[0]
    slope = (recent[-1] - recent[-3]) * 3
    vol = max(recent) - min(recent)
    m_score = max(min(momentum*2,1),-1)
    s_score = max(min(slope*1.5,1),-1)
    v_score = max(min(vol*BREAKOUT_FACTOR,1),0)
    combined = (m_score + s_score + v_score)/3

    if combined > SNIPER_CONF and trend=="BULL":
        return "CALL"
    elif combined < -SNIPER_CONF and trend=="BEAR":
        return "PUT"
    return None

def attempt_sniper_trade():
    global last_sniper_entry, trade_in_progress
    now = time.time()
    if trade_in_progress or now - last_sniper_entry < COOLDOWN_SECONDS:
        return
    ct = calculate_sniper_signal()
    if not ct:
        return
    last_sniper_entry = now
    trade_in_progress = True
    ws.send(json.dumps({
        "proposal":1,
        "amount": BASE_STAKE,
        "basis":"stake",
        "contract_type": ct,
        "currency":"USD",
        "duration":1,
        "duration_unit":"t",
        "symbol": SYMBOL
    }))
    alert(f"🎯 SNIPER ENTRY → {ct} | Stake {BASE_STAKE}")

# ================= SETTLEMENT =================
def settle(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    profit = float(c.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)
    TRADE_COUNT +=1
    trade_in_progress = False
    if profit>0:
        WINS+=1
        alert(f"✔ WIN +{profit:.2f}")
    else:
        LOSSES+=1
        alert(f"❌ LOSS {profit:.2f}")

# ================= WEBSOCKET =================
def on_msg(ws,msg):
    global BALANCE
    try:
        data = json.loads(msg)
        t = data.get("msg_type")

        if t=="authorize":
            alert("🔐 Authorized")
            ws.send(json.dumps({"ticks": SYMBOL,"subscribe":1}))
            ws.send(json.dumps({"proposal_open_contract":1,"subscribe":1}))
            ws.send(json.dumps({"balance":1,"subscribe":1}))

        elif t=="tick":
            price = float(data["tick"]["quote"])
            tick_buffer.append(price)
            price_buffer.append(price)
            attempt_sniper_trade()

        elif t=="proposal":
            pid = data["proposal"]["id"]
            ws.send(json.dumps({"buy": pid,"price": BASE_STAKE}))
            alert(f"💸 BUY ID {pid}")

        elif t=="proposal_open_contract":
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                settle(c)

        elif t=="balance":
            BALANCE = float(data["balance"]["balance"])
            equity_curve.append(BALANCE)

    except Exception as e:
        alert(f"⚠ WS Error {e}")

def on_open(ws):
    alert("🌐 Connected, authorizing…")
    ws.send(json.dumps({"authorize":API_TOKEN}))

def on_error(ws,err):
    alert(f"⚠ WS ERROR: {err}")

def on_close(ws,a,b):
    alert("🔌 Connection Closed, reconnecting…")
    time.sleep(2)
    start_ws()

# ================= START WS =================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_msg,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever,daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        try:
            ws.send(json.dumps({"ping":1}))
        except:
            pass

# ================= MAIN =================
alert("🚀 V10 SNIPER BOT — KOYEB CLOUD READY")
start_ws()
threading.Thread(target=keep_alive,daemon=True).start()

while True:
    time.sleep(1)