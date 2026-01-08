import os, json, threading, time, websocket, numpy as np
from collections import deque
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))

if not API_TOKEN:
    raise RuntimeError("Missing DERIV_API_TOKEN")

SYMBOL = "frxUSDJPY"
CURRENCY = "USD"

STAKE = 200
DURATION = 1
DURATION_UNIT = "m"

EMA_FAST = 9
EMA_SLOW = 21
FRACTAL_L = 2

MAX_HISTORY = 300
COOLDOWN = 60

LOSS_STREAK_LIMIT = 3
LOSS_PAUSE = 900  # seconds

# ================= STATE =================
prices = deque(maxlen=MAX_HISTORY)
trade_in_progress = False
last_trade_time = 0

LOSS_STREAK = 0
breaker_until = 0

BALANCE = 0.0
tick_count = 0
last_heartbeat = 0

ws = None

# ================= UTILS =================
def ts():
    return datetime.now().strftime("%H:%M:%S")

# ================= INDICATORS =================
def ema(data, p):
    if len(data) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(data[-p:], w, mode="valid")[0]

def fractal(data):
    L = FRACTAL_L
    if len(data) < (2 * L + 1):
        return None
    mid = data[-L - 1]
    left = data[-(2 * L + 1):-L - 1]
    right = data[-L:]
    if mid > max(left + right):
        return "UP"
    if mid < min(left + right):
        return "DOWN"
    return None

# ================= TRADING =================
def evaluate():
    global trade_in_progress, last_trade_time

    now = time.time()

    if trade_in_progress:
        return
    if now < breaker_until:
        return
    if now - last_trade_time < COOLDOWN:
        return

    fast = ema(list(prices), EMA_FAST)
    slow = ema(list(prices), EMA_SLOW)
    f = fractal(list(prices))

    if not fast or not slow or not f:
        return

    direction = None
    if f == "UP" and fast > slow:
        direction = "CALL"
    elif f == "DOWN" and fast < slow:
        direction = "PUT"

    if not direction:
        return

    send_proposal(direction)
    trade_in_progress = True
    last_trade_time = now

def send_proposal(direction):
    ws.send(json.dumps({
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": direction,
        "currency": CURRENCY,
        "duration": DURATION,
        "duration_unit": DURATION_UNIT,
        "symbol": SYMBOL
    }))
    print(f"{ts()} 📨 Proposal {direction}")

# ================= SETTLEMENT =================
def settle(c):
    global trade_in_progress, LOSS_STREAK, breaker_until, BALANCE

    profit = float(c.get("profit", 0))
    BALANCE = float(c.get("balance_after", BALANCE))

    if profit <= 0:
        LOSS_STREAK += 1
        print(f"{ts()} ❌ Loss streak {LOSS_STREAK}")
    else:
        LOSS_STREAK = 0

    print(f"{ts()} ✔ Settled | P/L {profit:.2f} | Bal {BALANCE:.2f}")
    trade_in_progress = False

    if LOSS_STREAK >= LOSS_STREAK_LIMIT:
        breaker_until = time.time() + LOSS_PAUSE
        print(f"{ts()} 🔴 LOSS BREAKER ({LOSS_PAUSE//60} min)")

# ================= WS =================
def resub():
    ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))
    ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))
    print(f"{ts()} 📡 Subscribed {SYMBOL}")

def start_ws():
    global ws
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    print(f"{ts()} Connecting {url}")

    def on_open(w):
        print(f"{ts()} ✅ Connected")
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        global tick_count, last_heartbeat, BALANCE
        try:
            d = json.loads(msg)

            if "authorize" in d and not d["authorize"].get("error"):
                resub()

            if "tick" in d:
                price = float(d["tick"]["quote"])
                prices.append(price)
                tick_count += 1
                evaluate()

                now = time.time()
                if now - last_heartbeat > 10:
                    print(f"{ts()} ❤️ Heartbeat | Ticks {tick_count}")
                    last_heartbeat = now

            if "proposal" in d:
                time.sleep(2)
                w.send(json.dumps({"buy": d["proposal"]["id"], "price": STAKE}))

            if "proposal_open_contract" in d:
                c = d["proposal_open_contract"]
                if c.get("is_sold") or c.get("is_expired"):
                    settle(c)

            if "balance" in d:
                BALANCE = float(d["balance"]["balance"])

        except Exception as e:
            print(f"{ts()} ❌ on_message error {e}")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= MAIN =================
if __name__ == "__main__":
    print("🚀 ULTRA-ELITE USDJPY BOT — KOYEB READY")
    while True:
        try:
            start_ws()
            while True:
                time.sleep(5)
        except Exception as e:
            print(f"{ts()} ❌ WS ERROR {e}, reconnecting")
            time.sleep(3)