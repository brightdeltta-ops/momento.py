import os
import json
import time
import threading
import websocket
from collections import deque

# ================= CONFIG =================
TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))

SYMBOL = "R_75"
RISK = 0.02
ENTRY_COOLDOWN = 25
MAX_CONSECUTIVE_LOSSES = 12

# ================= STATE =================
ticks = deque(maxlen=200)
balance = 0.0
authorized = False
trade_open = False
last_trade_time = 0
consecutive_losses = 0
shutdown = False
ws = None

# ================= UTILS =================
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

# ================= WS HANDLERS =================
def on_open(ws):
    ws.send(json.dumps({
        "authorize": TOKEN,
        "app_id": APP_ID
    }))

def on_message(ws, message):
    global authorized, balance, trade_open, consecutive_losses

    data = json.loads(message)

    if "authorize" in data:
        authorized = True
        log("✅ Authorized")
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)

    if "balance" in data:
        balance = float(data["balance"]["balance"])

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit", 0))
            trade_open = False
            if profit < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            log(f"✔ Trade settled | P/L {profit:.2f} | Balance {balance:.2f}")

def on_error(ws, error):
    log(f"❌ WS Error {error}")

def on_close(ws, *_):
    log("❌ WS Closed — reconnecting")
    time.sleep(2)
    start_ws()

# ================= TRADE LOGIC =================
def place_trade(direction):
    global trade_open, last_trade_time

    if trade_open:
        return

    stake = round(balance * RISK, 2)
    if stake < 0.35:
        return

    ws.send(json.dumps({
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": "CALL" if direction == "BUY" else "PUT",
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

    trade_open = True
    last_trade_time = time.time()
    log(f"📈 Trade fired {direction} | Stake {stake}")

def engine():
    global shutdown

    while not shutdown:
        if not authorized or len(ticks) < 60:
            time.sleep(0.5)
            continue

        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            log("🛑 Max losses hit — stopping")
            shutdown = True
            break

        if time.time() - last_trade_time < ENTRY_COOLDOWN:
            time.sleep(0.5)
            continue

        ema_fast = ema(list(ticks)[-30:], 10)
        ema_slow = ema(list(ticks)[-60:], 30)

        if not ema_fast or not ema_slow:
            continue

        if ema_fast > ema_slow:
            place_trade("BUY")
        elif ema_fast < ema_slow:
            place_trade("SELL")

        time.sleep(0.5)

# ================= START =================
def start_ws():
    global ws
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

log("🚀 MOMENTO BOT STARTING (KOYEB SAFE)")
start_ws()
threading.Thread(target=engine, daemon=True).start()

while not shutdown:
    time.sleep(1)