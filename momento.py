import json
import threading
import time
import websocket
import os
from datetime import datetime

# ================== ENV CONFIG ==================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = os.getenv("APP_ID", "1089")
SYMBOL = os.getenv("SYMBOL", "R_100")
STAKE = float(os.getenv("STAKE", "200"))

WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================== BOT STATE ==================
ws = None
trade_in_progress = False
trade_start_ts = 0
TRADE_TIMEOUT = 10

wins = 0
losses = 0
balance = 0.0
edge = 0.0

last_price = None
trend_score = 0.0

# ================== LOG ==================
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}", flush=True)

# ================== DERIV HELPERS ==================
def authorize():
    ws.send(json.dumps({"authorize": API_TOKEN}))

def request_balance():
    ws.send(json.dumps({"balance": 1, "subscribe": 1}))

def request_ticks():
    ws.send(json.dumps({
        "ticks": SYMBOL,
        "subscribe": 1
    }))

def request_proposal(direction):
    global trade_start_ts
    trade_start_ts = time.time()
    log(f"📨 PROPOSAL {direction} | stake={STAKE}")
    ws.send(json.dumps({
        "proposal": 1,
        "amount": STAKE,
        "basis": "stake",
        "contract_type": direction,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

def send_buy(pid, price):
    global trade_in_progress
    trade_in_progress = True
    ws.send(json.dumps({
        "buy": pid,
        "price": price
    }))
    log(f"🔥 BUY SENT | price={price}")

# ================== STRATEGY ==================
def analyze_tick(price):
    global last_price, trend_score

    if last_price is None:
        last_price = price
        return None, 0.0

    delta = price - last_price
    last_price = price

    trend_score = 0.8 * trend_score + 0.2 * delta
    confidence = min(abs(trend_score) * 50, 1.0)

    if confidence < 0.55:
        return None, confidence

    return ("CALL" if trend_score > 0 else "PUT"), confidence

# ================== WATCHDOG ==================
def trade_watchdog():
    global trade_in_progress
    while True:
        if trade_in_progress and time.time() - trade_start_ts > TRADE_TIMEOUT:
            log("⚠ TRADE TIMEOUT — FORCE UNLOCK")
            trade_in_progress = False
        time.sleep(1)

# ================== WS HANDLER ==================
def on_message(ws_, message):
    global trade_in_progress, wins, losses, balance, edge

    data = json.loads(message)

    if "authorize" in data:
        log("CONNECTED & AUTHORIZED")
        request_balance()
        request_ticks()

    elif "balance" in data:
        balance = float(data["balance"]["balance"])

    elif "tick" in data:
        price = float(data["tick"]["quote"])

        if trade_in_progress:
            return

        direction, conf = analyze_tick(price)
        if direction:
            log(f"TREND {direction} | Conf {conf:.2f}")
            request_proposal(direction)

    elif "proposal" in data:
        pid = data["proposal"]["id"]
        ask_price = float(data["proposal"]["ask_price"])
        send_buy(pid, ask_price)

    elif "buy" in data:
        cid = data["buy"]["contract_id"]
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": cid,
            "subscribe": 1
        }))

    elif "proposal_open_contract" in data:
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            pnl = float(poc.get("profit", 0))
            trade_in_progress = False

            if pnl > 0:
                wins += 1
            else:
                losses += 1

            total = wins + losses
            edge = (wins - losses) / total if total > 0 else 0.0

            log(f"✔ SETTLED | P/L={pnl:.2f} | W={wins} L={losses} EDGE={edge:.2f}")

    elif "error" in data:
        log(f"❌ ERROR: {data['error']['message']}")
        trade_in_progress = False

def on_open(ws_):
    authorize()

def on_close(ws_, *_):
    log("🔴 WS CLOSED — RECONNECTING")
    time.sleep(2)
    start()

def on_error(ws_, err):
    log(f"❌ WS ERROR: {err}")

# ================== START ==================
def start():
    global ws
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_open=on_open,
        on_close=on_close,
        on_error=on_error
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)

if __name__ == "__main__":
    log("🚀 MOMENTO — FIRING BOT (KOYEB READY)")
    log(f"❤️ BAL={balance:.2f} W={wins} L={losses} EDGE={edge:.2f}")
    threading.Thread(target=trade_watchdog, daemon=True).start()
    start()