import json, threading, time, websocket, numpy as np
from collections import deque
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button

# =================================================
# USER CONFIG
# =================================================
API_TOKEN = "eZjDrK54yMTAsbf"
APP_ID = 112380
SYMBOL = "R_75"

BASE_STAKE = 1.0
TRADE_DURATION = 1  # ticks

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_LOOKBACK = 5

# =================================================
# BUFFERS & STATE
# =================================================
tick_history = deque(maxlen=500)
trade_queue = deque(maxlen=10)
active_trades = []

BALANCE = 0.0
WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_proposal_time = 0

authorized = False
ws_running = False
ws = None
lock = threading.Lock()

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# =================================================
# GUI
# =================================================
plt.style.use("dark_background")
fig, ax = plt.subplots(figsize=(13, 6))
fig.subplots_adjust(left=0.18, right=0.97, top=0.62, bottom=0.05)

btn_ax = plt.axes([0.02, 0.02, 0.4, 0.06])
btn = Button(btn_ax, "🚨 STOP", color=(1, 0, 0, 0.6), hovercolor="darkred")

def emergency_stop(event):
    global trade_in_progress
    trade_in_progress = False
    append_alert("🚨 EMERGENCY STOP ACTIVATED")

btn.on_clicked(emergency_stop)

equity_curve = []
alerts = deque(maxlen=50)

# =================================================
# UTILITIES
# =================================================
def append_alert(msg):
    with lock:
        alerts.appendleft(msg)
    print(msg)

def session_loss_check():
    return LOSSES * BASE_STAKE < 50

def auto_unfreeze():
    global trade_in_progress
    while True:
        time.sleep(2)
        if trade_in_progress and time.time() - last_trade_time > 5:
            trade_in_progress = False
            append_alert("⚠ Auto-unfreeze triggered")

# =================================================
# STRATEGY CORE
# =================================================
def calculate_ema(prices, period):
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]

def micro_fractal():
    if len(tick_history) < FRACTAL_LOOKBACK + 2:
        return None

    recent = list(tick_history)[-FRACTAL_LOOKBACK-2:-2]
    high = max(recent)
    low = min(recent)
    current = tick_history[-1]

    if current > high:
        return "up"
    if current < low:
        return "down"
    return None

def trade_signal():
    if len(tick_history) < EMA_SLOW + 5:
        return None

    prices = list(tick_history)
    price = prices[-1]

    ema_fast = calculate_ema(prices, EMA_FAST)
    ema_slow = calculate_ema(prices, EMA_SLOW)

    fractal = micro_fractal()
    if not fractal:
        return None

    # BUY
    if fractal == "up" and ema_fast > ema_slow and price > ema_fast:
        return "up"

    # SELL
    if fractal == "down" and ema_fast < ema_slow and price < ema_fast:
        return "down"

    return None

def evaluate_and_trade():
    global last_proposal_time

    now = time.time()
    if trade_in_progress:
        return
    if now - last_proposal_time < 0.4:
        return
    if not session_loss_check():
        return

    direction = trade_signal()
    if not direction:
        return

    trade_queue.append((direction, TRADE_DURATION, BASE_STAKE))
    last_proposal_time = now
    append_alert(f"🎯 FRACTAL + EMA → {direction.upper()} | Stake {BASE_STAKE}")
    process_trade_queue()

# =================================================
# EXECUTION
# =================================================
def process_trade_queue():
    global trade_in_progress
    if trade_queue and not trade_in_progress:
        request_proposal(trade_queue.popleft())

def request_proposal(signal):
    global trade_in_progress, last_trade_time

    direction, duration, stake = signal
    ct = "CALL" if direction == "up" else "PUT"

    proposal = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": duration,
        "duration_unit": "t",
        "symbol": SYMBOL
    }

    trade_in_progress = True
    last_trade_time = time.time()
    ws.send(json.dumps(proposal))
    append_alert(f"📨 Proposal → {direction.upper()}")

def on_contract_settlement(c):
    global BALANCE, WINS, LOSSES, TRADE_COUNT, trade_in_progress

    profit = float(c.get("profit") or 0)
    BALANCE += profit
    equity_curve.append(BALANCE)

    if profit > 0:
        WINS += 1
    else:
        LOSSES += 1

    TRADE_COUNT += 1
    trade_in_progress = False

    append_alert(f"✔ Closed | P/L {profit:.2f}")
    process_trade_queue()

# =================================================
# WEBSOCKET HANDLER
# =================================================
def on_message(ws, msg):
    global BALANCE, authorized
    try:
        data = json.loads(msg)

        if "authorize" in data:
            authorized = True
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
            append_alert("✔ Authorized")

        if "tick" in data:
            tick = float(data["tick"]["quote"])
            tick_history.append(tick)
            evaluate_and_trade()

        if "proposal" in data:
            pid = data["proposal"]["id"]
            ws.send(json.dumps({"buy": pid, "price": BASE_STAKE}))
            active_trades.append(pid)

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold") or c.get("is_expired"):
                on_contract_settlement(c)

        if "balance" in data:
            BALANCE = float(data["balance"]["balance"])
            equity_curve.append(BALANCE)

    except Exception as e:
        append_alert(f"⚠ WS Error: {e}")

# =================================================
# GUI ANIMATION
# =================================================
def animate(_):
    with lock:
        if len(tick_history) < 5:
            return

        ax.clear()
        ax.plot(list(tick_history), color="white")
        if equity_curve:
            ax.plot(equity_curve, color="lime", linestyle="--")

        stats = (
            f"BAL: {BALANCE:.2f}\n"
            f"W: {WINS} | L: {LOSSES}\n"
            f"TRADES: {TRADE_COUNT}\n"
            f"STAKE: {BASE_STAKE:.2f}\n"
            f"OPEN: {1 if trade_in_progress else 0}"
        )

        ax.text(
            0.02, 0.88, stats,
            transform=ax.transAxes,
            color="lime",
            fontsize=12,
            bbox=dict(facecolor=(0, 0.15, 0, 0.4), edgecolor="lime")
        )

        for i, msg in enumerate(list(alerts)):
            ax.text(
                0.02, 0.72 - i * 0.05,
                msg,
                transform=ax.transAxes,
                fontsize=9,
                color="cyan",
                bbox=dict(facecolor=(0, 0.2, 0, 0.4), edgecolor="none")
            )

ani = FuncAnimation(fig, animate, interval=60)

# =================================================
# STARTUP
# =================================================
def start_ws():
    global ws, ws_running
    if ws_running:
        return
    ws_running = True

    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=lambda ws: ws.send(json.dumps({"authorize": API_TOKEN})),
        on_message=on_message,
        on_error=lambda ws, err: append_alert(f"❌ WS Error: {err}"),
        on_close=lambda *args: append_alert("❌ WS Closed")
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

def keep_alive():
    while True:
        time.sleep(15)
        try:
            ws.send(json.dumps({"ping": 1}))
        except:
            pass

def start():
    start_ws()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=auto_unfreeze, daemon=True).start()
    plt.show()

if __name__ == "__main__":
    start()