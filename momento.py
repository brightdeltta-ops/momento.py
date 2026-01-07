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

BASE_STAKE = 200.0
MAX_STAKE = 10000.0
RISK_FRAC = 0.02

EMA_FAST = 3
EMA_SLOW = 10
MICRO_SLICE = 10

VOL_THRESHOLD_BASE = 0.0015
CONF_MIN = 0.5
COOLDOWN = 10

# ================= STATE =================
ticks = deque(maxlen=500)
micro = deque(maxlen=MICRO_SLICE)

balance = 0.0
wins = losses = trades = 0
trade_open = False
last_trade_time = 0

current_regime = "idle"
last_signal = "-"

shutdown = False
ws = None

# ================= UTILS =================
def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    val = data[0]
    for p in data[1:]:
        val = p * k + val * (1 - k)
    return val

def volatility_threshold():
    if len(ticks) < 50:
        return VOL_THRESHOLD_BASE
    diffs = [abs(ticks[i] - ticks[i-1]) for i in range(1, len(ticks))]
    diffs.sort()
    idx = int(len(diffs) * 0.6)
    return diffs[idx]

def dynamic_stake(conf):
    stake = BASE_STAKE + conf * balance * RISK_FRAC
    return min(round(stake, 2), MAX_STAKE)

# ================= REGIME =================
def detect_regime():
    if len(micro) < EMA_SLOW:
        return "idle", 0.0

    ef = ema(list(micro)[-EMA_FAST:], EMA_FAST)
    es = ema(list(micro)[-EMA_SLOW:], EMA_SLOW)

    if ef is None or es is None:
        return "idle", 0.0

    vol = max(micro) - min(micro)
    vol_th = volatility_threshold()
    diff = abs(ef - es)

    if vol >= vol_th and diff > vol * 0.3:
        return "trend", min(1.0, diff / vol)

    if vol < vol_th * 0.7:
        return "compression", min(1.0, (vol_th - vol) / vol_th)

    return "idle", 0.0

# ================= STRATEGY =================
def strategy(regime, conf):
    ef = ema(list(micro)[-EMA_FAST:], EMA_FAST)
    es = ema(list(micro)[-EMA_SLOW:], EMA_SLOW)

    if ef is None or es is None:
        return None, 0

    if regime == "trend":
        if ef > es:
            return "up", conf
        if ef < es:
            return "down", conf

    if regime == "compression":
        hi = max(micro)
        lo = min(micro)
        last = micro[-1]
        rng = hi - lo
        if rng == 0:
            return None, 0
        if last > hi - 0.15 * rng:
            return "up", conf
        if last < lo + 0.15 * rng:
            return "down", conf

    return None, 0

# ================= WS HANDLERS =================
def on_open(ws):
    ws.send(json.dumps({"authorize": TOKEN}))

def on_message(ws, message):
    global balance, trade_open, wins, losses, trades
    global current_regime, last_signal, last_trade_time

    data = json.loads(message)

    if "authorize" in data:
        log("✅ Authorized")
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

    if "tick" in data:
        price = float(data["tick"]["quote"])
        ticks.append(price)
        micro.append(price)

        if trade_open:
            return

        if time.time() - last_trade_time < COOLDOWN:
            return

        regime, conf = detect_regime()
        current_regime = regime

        if conf < CONF_MIN:
            return

        direction, conf = strategy(regime, conf)
        if not direction:
            return

        stake = dynamic_stake(conf)
        ct = "CALL" if direction == "up" else "PUT"

        ws.send(json.dumps({
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": ct,
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",
            "symbol": SYMBOL
        }))

        trade_open = True
        last_trade_time = time.time()
        last_signal = f"{regime.upper()} {direction.upper()}"
        log(f"📈 {last_signal} | Stake {stake}")

    if "proposal" in data:
        ws.send(json.dumps({
            "buy": data["proposal"]["id"],
            "price": data["proposal"]["ask_price"]
        }))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            profit = float(c.get("profit") or 0)
            trades += 1
            trade_open = False

            if profit > 0:
                wins += 1
            else:
                losses += 1

            log(f"✔ Settled | P/L {profit:.2f} | Balance {balance:.2f}")

    if "balance" in data:
        balance = float(data["balance"]["balance"])

def on_error(ws, error):
    log(f"❌ WS Error {error}")

def on_close(ws, *_):
    log("❌ WS Closed — reconnecting")
    time.sleep(2)
    start_ws()

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

log("🚀 REGIME BOT STARTING (KOYEB SAFE)")
start_ws()

while True:
    time.sleep(1)