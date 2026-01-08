import json, time, websocket, numpy as np, os
from collections import deque

# ================= ENV =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "112380"))

CURRENCY = "USD"

# ================= SYMBOLS =================
V10 = "R_10"
V75 = "R_75"
ACTIVE_SYMBOL = V10

# ================= RISK =================
BASE_STAKE = 1.0
STAKE_UP = 1.15
MAX_STAKE_MULT = 3.0
TRADE_DURATION = 1

# ================= STATE =================
tick_history = deque(maxlen=300)

BALANCE = 0
WINS = LOSSES = TRADES = 0
current_stake = BASE_STAKE

trade_in_progress = False
ws = None

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= LOG =================
def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

# ================= MATH =================
def ema(prices, period):
    if len(prices) < period:
        return None
    w = np.exp(np.linspace(-1., 0., period))
    w /= w.sum()
    return np.dot(prices[-period:], w)

def volatility_score():
    if len(tick_history) < 20:
        return 0
    r = list(tick_history)[-20:]
    return max(r) - min(r)

def sniper_confidence():
    if len(tick_history) < 6:
        return 0
    r = list(tick_history)[-6:]
    momentum = abs(r[-1] - r[0])
    slope = abs(r[-1] - r[-3])
    vol = max(r) - min(r)
    return min((momentum + slope + vol) * 5, 1)

def micro_fractal():
    if len(tick_history) < 7:
        return None
    r = list(tick_history)[-7:]
    if r[-1] > max(r[:-1]): return "up"
    if r[-1] < min(r[:-1]): return "down"
    return None

# ================= REGIME SWITCH =================
def update_symbol():
    global ACTIVE_SYMBOL, tick_history
    v = volatility_score()

    # Threshold tuned for Deriv synthetics
    new_symbol = V75 if v > 6 else V10

    if new_symbol != ACTIVE_SYMBOL:
        ACTIVE_SYMBOL = new_symbol
        tick_history.clear()
        ws.send(json.dumps({"forget_all": "ticks"}))
        ws.send(json.dumps({"ticks": ACTIVE_SYMBOL, "subscribe": 1}))
        log(f"🔁 SWITCHED TO {ACTIVE_SYMBOL}")

# ================= STRATEGY =================
def signal():
    prices = list(tick_history)

    if ACTIVE_SYMBOL == V10:
        ef, es, conf_min = 6, 14, 0.35
    else:
        ef, es, conf_min = 9, 21, 0.55

    ema_f = ema(prices, ef)
    ema_s = ema(prices, es)
    if ema_f is None or ema_s is None:
        return None

    fractal = micro_fractal()
    conf = sniper_confidence()

    if conf < conf_min:
        return None

    if fractal == "up" and ema_f > ema_s:
        return "CALL"
    if fractal == "down" and ema_f < ema_s:
        return "PUT"

    return None

# ================= EXECUTION =================
def evaluate():
    global trade_in_progress
    if trade_in_progress:
        return
    sig = signal()
    if sig:
        buy(sig)

def buy(ct):
    global trade_in_progress
    trade_in_progress = True

    ws.send(json.dumps({
        "proposal": 1,
        "amount": round(current_stake, 2),
        "basis": "stake",
        "contract_type": ct,
        "currency": CURRENCY,
        "duration": TRADE_DURATION,
        "duration_unit": "t",
        "symbol": ACTIVE_SYMBOL
    }))

    log(f"🎯 {ACTIVE_SYMBOL} {ct} | STAKE {current_stake:.2f}")

def settle(c):
    global WINS, LOSSES, TRADES, trade_in_progress, current_stake

    profit = float(c.get("profit") or 0)
    trade_in_progress = False
    TRADES += 1

    if profit > 0:
        WINS += 1
        current_stake = min(current_stake * STAKE_UP, BASE_STAKE * MAX_STAKE_MULT)
        log(f"✔ WIN {profit:.2f} → {current_stake:.2f}")
    else:
        LOSSES += 1
        current_stake = BASE_STAKE
        log(f"❌ LOSS {profit:.2f} → RESET")

# ================= WS =================
def on_message(ws, msg):
    data = json.loads(msg)

    if "authorize" in data:
        ws.send(json.dumps({"ticks": ACTIVE_SYMBOL, "subscribe": 1}))
        ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
        log("🔐 AUTHORIZED")

    if "tick" in data:
        tick_history.append(float(data["tick"]["quote"]))
        update_symbol()
        evaluate()

    if "proposal" in data:
        ws.send(json.dumps({"buy": data["proposal"]["id"], "price": current_stake}))

    if "proposal_open_contract" in data:
        c = data["proposal_open_contract"]
        if c.get("is_sold"):
            settle(c)

def on_open(ws):
    log("🌐 CONNECTED")
    ws.send(json.dumps({"authorize": API_TOKEN}))

# ================= RUN =================
if __name__ == "__main__":
    log("🚀 DUAL SYMBOL BOT — V10 ⇄ V75 ACTIVE")
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message
    )
    ws.run_forever()