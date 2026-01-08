import json, threading, time, websocket, numpy as np
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import os  # for environment variable

# =====================================================
# CONFIG
# =====================================================
API_TOKEN = os.getenv("DERIV_API_TOKEN")  # Use env variable on Koyeb
if not API_TOKEN:
    raise ValueError("DERIV_API_TOKEN environment variable not set!")

APP_ID = 112380
SYMBOL = "R_10"      # V10 1s sniper recommended

BASE_STAKE = 250.0
COOLDOWN_SECONDS = 6
BREAKOUT_FACTOR = 1.8
SMOOTHING_WEIGHT = 5
SNIPER_CONF = 0.92

# =====================================================
# BUFFERS & STATE
# =====================================================
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
processed_contracts = set()

ws = None
lock = threading.Lock()

# =====================================================
# ALERT
# =====================================================
def alert(msg):
    with lock:
        alerts.appendleft(msg)
    print(msg, flush=True)

# =====================================================
# EMA CALCULATION
# =====================================================
def calc_ema(prices, period=20):
    if len(prices) < period:
        return None
    weights = np.exp(np.linspace(-1.,0.,period))
    weights /= weights.sum()
    return np.convolve(prices, weights, mode='valid')[-1]

# =====================================================
# SNIPER LOGIC
# =====================================================
def smooth_tick(last_value, new_value):
    return (last_value * (SMOOTHING_WEIGHT - 1) + new_value) / SMOOTHING_WEIGHT

def calculate_sniper_signal():
    if len(tick_buffer) < 8 or len(price_buffer) < 20:
        return None

    ema = calc_ema(list(price_buffer))
    current_price = tick_buffer[-1]
    if ema is None:
        return None

    trend = "BULL" if current_price > ema else "BEAR"

    smoothed = [smooth_tick(tick_buffer[i-1], tick_buffer[i]) for i in range(1,len(tick_buffer))]
    recent = smoothed[-5:]
    momentum = recent[-1] - recent[0]
    slope = (recent[-1] - recent[-3]) * 3
    vol = max(recent) - min(recent)
    m_score = min(max(momentum * 2, -1),1)
    s_score = min(max(slope * 1.5,-1),1)
    v_score = min(max(vol * BREAKOUT_FACTOR,0),1)
    combined = (m_score + s_score + v_score)/3

    if combined > SNIPER_CONF and trend=="BULL":
        return "BUY"
    elif combined < -SNIPER_CONF and trend=="BEAR":
        return "SELL"
    else:
        return None

def attempt_sniper_trade():
    global last_sniper_entry, trade_in_progress
    try:
        now = time.time()
        if trade_in_progress or now - last_sniper_entry < COOLDOWN_SECONDS:
            return

        signal = calculate_sniper_signal()
        if not signal:
            return

        last_sniper_entry = now
        stake = BASE_STAKE
        ct = "CALL" if signal=="BUY" else "PUT"

        proposal = {
            "proposal":1,
            "amount": stake,
            "basis":"stake",
            "contract_type": ct,
            "currency":"USD",
            "duration":1,
            "duration_unit":"t",
            "symbol": SYMBOL
        }

        ws.send(json.dumps(proposal))
        trade_in_progress = True
        alert(f"🎯 SNIPER ENTRY → {ct} | Stake {stake}")
    except Exception as e:
        alert(f"⚠ Attempt trade error: {e}")

# =====================================================
# SETTLEMENT
# =====================================================
def settle(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress
    try:
        pid = c.get("contract_id") or c.get("id")
        if not pid or pid in processed_contracts:
            return
        processed_contracts.add(pid)

        profit = float(c.get("profit") or 0)
        BALANCE += profit
        equity_curve.append(BALANCE)
        TRADE_COUNT +=1
        trade_in_progress = False

        if profit>0:
            WINS +=1
            alert(f"✔ WIN +{profit:.2f}")
        else:
            LOSSES +=1
            alert(f"❌ LOSS {profit:.2f}")
    except Exception as e:
        alert(f"⚠ Settlement error: {e}")

# =====================================================
# WEBSOCKET HANDLER
# =====================================================
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
    alert("🔌 Connection Closed")

# =====================================================
# START WEBSOCKET
# =====================================================
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

# =====================================================
# GUI / EQUITY PLOT
# =====================================================
plt.style.use("dark_background")
fig, ax = plt.subplots(figsize=(12,6))
fig.subplots_adjust(left=0.15,right=0.95,top=0.65)

def animate(_):
    ax.clear()
    ax.set_facecolor("black")
    if equity_curve: ax.plot(equity_curve,color="lime")
    stats = (
        f"BAL: {BALANCE:.2f}\n"
        f"WINS: {WINS} | LOSSES: {LOSSES}\n"
        f"TRADES: {TRADE_COUNT}"
    )
    ax.text(0.02,0.85,stats,transform=ax.transAxes,color="lime")
    for i,msg in enumerate(alerts):
        ax.text(0.02,0.75-i*0.05,msg,transform=ax.transAxes,color="cyan")

ani = FuncAnimation(fig,animate,interval=80)

# =====================================================
# START BOT
# =====================================================
def start():
    start_ws()
    threading.Thread(target=keep_alive,daemon=True).start()
    plt.show()

if __name__=="__main__":
    alert("🚀 V10 SNIPER BOT — KOYEB CLOUD READY")
    start()