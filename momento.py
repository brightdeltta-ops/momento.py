import json
import time
import threading
import websocket
import numpy as np
from collections import deque
from rich.console import Console

# ================= CONFIG =================
API_TOKEN = "PUT_YOUR_DERIV_TOKEN_HERE"
APP_ID = 112380
SYMBOL = "R_75"

BASE_STAKE = 1.0
MAX_STAKE = 50.0

MICRO_SLICE = 60
EMA_FAST_BASE = 10
EMA_SLOW_BASE = 30

CONF_THRESHOLD = 0.60
RECOVERY_CONF = 0.75

TRADE_COOLDOWN = 8

MAX_RECOVERY_STEPS = 3
MAX_RECOVERY_MULT = 2.5
# =========================================

console = Console()

tick_buffer = deque(maxlen=MICRO_SLICE)
last_trade_time = 0
armed = False

# ---- Loss recovery state ----
loss_bank = 0.0
recovery_step = 0

# ================= EMA =================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    alpha = 2 / (period + 1)
    ema = data[0]
    for p in data[1:]:
        ema = alpha * p + (1 - alpha) * ema
    return ema

# ================= Adaptive EMA =================
def adaptive_ema_periods(arr):
    vol = np.std(arr)
    if vol < 0.3:
        return EMA_FAST_BASE + 5, EMA_SLOW_BASE + 10
    if vol > 1.2:
        return max(5, EMA_FAST_BASE - 3), max(12, EMA_SLOW_BASE - 10)
    return EMA_FAST_BASE, EMA_SLOW_BASE

# ================= Features =================
def extract_features():
    global armed

    if len(tick_buffer) < MICRO_SLICE:
        armed = False
        return None

    arr = np.array(tick_buffer)
    ema_fast_p, ema_slow_p = adaptive_ema_periods(arr)

    ema_slow_p = min(ema_slow_p, len(arr) - 1)

    ema_fast = calculate_ema(arr, ema_fast_p)
    ema_slow = calculate_ema(arr, ema_slow_p)

    if ema_fast is None or ema_slow is None:
        armed = False
        return None

    armed = True
    return np.array([
        ema_fast - ema_slow,
        arr[-1] - arr[0],
        np.std(arr)
    ])

# ================= Confidence =================
weights = np.array([0.5, 0.3, 0.2])

def learner_confidence(features):
    score = np.dot(weights, features)
    return 1 / (1 + np.exp(-score))

# ================= Stake + Recovery =================
def calculate_stake(conf):
    global loss_bank, recovery_step

    stake = BASE_STAKE * (1 + conf)

    if loss_bank > 0 and conf >= RECOVERY_CONF:
        boost = min(loss_bank / 2, BASE_STAKE * 1.5)
        stake += boost
        recovery_step += 1

    stake = min(stake, BASE_STAKE * MAX_RECOVERY_MULT)
    stake = min(stake, MAX_STAKE)

    if recovery_step >= MAX_RECOVERY_STEPS:
        loss_bank = 0
        recovery_step = 0

    return max(BASE_STAKE, stake)

# ================= Trade Result =================
def on_trade_result(pnl):
    global loss_bank, recovery_step

    if pnl < 0:
        loss_bank += abs(pnl)
        console.log(f"[red]❌ LOSS → bank={loss_bank:.2f}[/red]")
    else:
        console.log(f"[green]✅ WIN → recovered {loss_bank:.2f}[/green]")
        loss_bank = 0
        recovery_step = 0

# ================= Trade Logic =================
def maybe_trade(price):
    global last_trade_time

    if time.time() - last_trade_time < TRADE_COOLDOWN:
        return

    features = extract_features()
    if features is None:
        console.log("[yellow]⚠ WARMING UP — EMA not ready[/yellow]")
        return

    conf = learner_confidence(features)
    if conf < CONF_THRESHOLD:
        return

    stake = calculate_stake(conf)
    direction = "CALL" if features[0] > 0 else "PUT"

    console.log(
        f"[bold green]🚀 TRADE → {direction} | "
        f"stake={stake:.2f} | conf={conf:.2f} | "
        f"bank={loss_bank:.2f}[/bold green]"
    )

    last_trade_time = time.time()

    # 🔴 DEMO RESULT (replace with real contract callback)
    simulated_pnl = np.random.choice([-stake, stake * 0.9])
    on_trade_result(simulated_pnl)

# ================= WebSocket =================
def on_message(ws, message):
    data = json.loads(message)
    if "tick" in data:
        price = float(data["tick"]["quote"])
        tick_buffer.append(price)
        console.log(f"[cyan]TICK {price:.4f}[/cyan]")
        maybe_trade(price)

def on_open(ws):
    console.log("[green]✅ Connected to Deriv[/green]")
    ws.send(json.dumps({"authorize": API_TOKEN, "app_id": APP_ID}))
    time.sleep(1)
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

def on_error(ws, error):
    console.log(f"[red]WS ERROR: {error}[/red]")

# ================= Heartbeat =================
def heartbeat():
    while True:
        state = "ARMED 🟢" if armed else "WARMING 🔵"
        console.log(
            f"❤️ HEARTBEAT → {state} | "
            f"ticks={len(tick_buffer)} | "
            f"bank={loss_bank:.2f} | "
            f"step={recovery_step}"
        )
        time.sleep(5)

# ================= START =================
console.log(
    f"[bold cyan]STARTING MOMENTO BOT → "
    f"BUFFER={MICRO_SLICE} EMA={EMA_FAST_BASE}/{EMA_SLOW_BASE}[/bold cyan]"
)

threading.Thread(target=heartbeat, daemon=True).start()

ws = websocket.WebSocketApp(
    "wss://ws.derivws.com/websockets/v3",
    on_open=on_open,
    on_message=on_message,
    on_error=on_error
)
ws.run_forever()