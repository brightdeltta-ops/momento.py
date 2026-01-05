import json
import time
import threading
import websocket
import numpy as np
from collections import deque
from rich.console import Console

# ================== USER CONFIG ==================
API_TOKEN = "PUT_YOUR_DERIV_TOKEN_HERE"
APP_ID = 112380               # MUST be valid
SYMBOL = "R_75"

BASE_STAKE = 0.35
MAX_STAKE = 2.0

FAST_EMA = 10
SLOW_EMA = 30
BUFFER_SIZE = 60

TRADE_COOLDOWN = 8            # seconds
CONF_THRESHOLD = 0.60
RECOVERY_CONF = 0.75

MAX_RECOVERY_STEPS = 3
MAX_RECOVERY_MULT = 2.5

RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60
TICK_TIMEOUT = 20
HEARTBEAT_INTERVAL = 5
# =================================================

console = Console()

# ================== STATE ==================
tick_buffer = deque(maxlen=BUFFER_SIZE)

ws = None
connected = False
last_tick_time = 0
last_trade_time = 0

trade_in_progress = False
loss_bank = 0.0
recovery_step = 0
armed = False

reconnect_delay = RECONNECT_DELAY
# ===========================================

# ================== EMA ==================
def calculate_ema(data, period):
    if len(data) < period:
        return None
    alpha = 2 / (period + 1)
    ema = data[0]
    for p in data[1:]:
        ema = alpha * p + (1 - alpha) * ema
    return ema

# ================== SIGNAL ==================
def extract_signal():
    global armed

    if len(tick_buffer) < BUFFER_SIZE:
        armed = False
        return None

    arr = np.array(tick_buffer)

    ema_fast = calculate_ema(arr, FAST_EMA)
    ema_slow = calculate_ema(arr, SLOW_EMA)

    if ema_fast is None or ema_slow is None:
        armed = False
        return None

    armed = True
    direction = "CALL" if ema_fast > ema_slow else "PUT"

    spread = abs(ema_fast - ema_slow)
    volatility = np.std(arr)
    confidence = min(0.95, spread / (volatility + 1e-6))

    return direction, confidence

# ================== STAKE ==================
def calculate_stake(conf):
    global loss_bank, recovery_step

    stake = BASE_STAKE * (1 + conf)

    if loss_bank > 0 and conf >= RECOVERY_CONF:
        stake += min(loss_bank * 0.5, BASE_STAKE * 1.5)
        recovery_step += 1

    if recovery_step >= MAX_RECOVERY_STEPS:
        loss_bank = 0
        recovery_step = 0

    stake = min(stake, BASE_STAKE * MAX_RECOVERY_MULT)
    stake = min(stake, MAX_STAKE)

    return round(max(BASE_STAKE, stake), 2)

# ================== TRADE ==================
def try_trade():
    global trade_in_progress, last_trade_time

    if trade_in_progress:
        return

    if time.time() - last_trade_time < TRADE_COOLDOWN:
        return

    signal = extract_signal()
    if not signal:
        return

    direction, conf = signal
    if conf < CONF_THRESHOLD:
        return

    stake = calculate_stake(conf)

    console.log(
        f"[bold green]📤 BUY {direction} | stake={stake:.2f} | conf={conf:.2f} | bank={loss_bank:.2f}[/bold green]"
    )

    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

    trade_in_progress = True
    last_trade_time = time.time()

# ================== RESULT ==================
def handle_contract(data):
    global trade_in_progress, loss_bank, recovery_step

    c = data["proposal_open_contract"]
    if not c.get("is_sold"):
        return

    profit = float(c["profit"])
    trade_in_progress = False

    if profit < 0:
        loss_bank += abs(profit)
        console.log(f"[red]❌ LOSS {profit:.2f} | bank={loss_bank:.2f}[/red]")
    else:
        console.log(f"[green]✅ WIN {profit:.2f} | recovered={loss_bank:.2f}[/green]")
        loss_bank = 0
        recovery_step = 0

# ================== WS HANDLERS ==================
def on_open(w):
    global connected, reconnect_delay
    connected = True
    reconnect_delay = RECONNECT_DELAY
    console.log("[green]✅ WebSocket OPEN[/green]")
    w.send(json.dumps({"authorize": API_TOKEN}))

def on_message(w, msg):
    global last_tick_time

    data = json.loads(msg)

    if "authorize" in data:
        console.log("[bold green]🔓 AUTHORIZED[/bold green]")
        w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        return

    if "tick" in data:
        price = float(data["tick"]["quote"])
        last_tick_time = time.time()
        tick_buffer.append(price)
        console.log(f"[cyan]TICK {price:.4f}[/cyan]")
        try_trade()

    if "proposal" in data:
        w.send(json.dumps({
            "buy": data["proposal"]["id"],
            "price": data["proposal"]["ask_price"]
        }))

    if "proposal_open_contract" in data:
        handle_contract(data)

def on_error(w, error):
    console.log(f"[red]WS ERROR: {error}[/red]")

def on_close(w, code, reason):
    global connected
    connected = False
    console.log(f"[red]WS CLOSED | {code} | {reason}[/red]")

# ================== CONNECT ==================
def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    console.log(f"[yellow]🌐 Connecting → {url}[/yellow]")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================== WATCHDOG ==================
def watchdog():
    global reconnect_delay, connected

    while True:
        now = time.time()

        if connected and now - last_tick_time > TICK_TIMEOUT:
            console.log("[bold red]⚠ TICK TIMEOUT → RECONNECT[/bold red]")
            try:
                ws.close()
            except:
                pass
            connected = False

        if not connected:
            console.log(f"[yellow]🔁 Reconnecting in {reconnect_delay}s[/yellow]")
            time.sleep(reconnect_delay)
            start_ws()
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

        state = "ARMED 🟢" if armed else "WARMING 🔵"
        console.log(
            f"❤️ HEARTBEAT | {state} | ticks={len(tick_buffer)} | bank={loss_bank:.2f} | step={recovery_step}"
        )

        time.sleep(HEARTBEAT_INTERVAL)

# ================== START ==================
if not API_TOKEN or not APP_ID:
    raise RuntimeError("❌ Missing API_TOKEN or APP_ID")

console.log("[bold cyan]🚀 MOMENTO BOT STARTING (REAL TRADING)[/bold cyan]")

threading.Thread(target=watchdog, daemon=True).start()
start_ws()

while True:
    time.sleep(10)