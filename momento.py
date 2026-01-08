import json
import time
import threading
import websocket
import os
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "1089"))

SYMBOL = "R_10"
DURATION = 1
BASE_STAKE = 200.0
MAX_STAKE = 250.0
COOLDOWN = 1.3

DERIV_WS = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# ================= STATE =================
ws = None
trade_in_progress = False
last_trade_time = 0
stake = BASE_STAKE
current_contract = None

# ================= LOG =================
def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)

# ================= TRADE LOGIC =================
def can_trade():
    return (not trade_in_progress) and (time.time() - last_trade_time > COOLDOWN)

def send_proposal(direction):
    global trade_in_progress, last_trade_time, stake

    if not can_trade():
        return

    trade_in_progress = True
    last_trade_time = time.time()
    stake = min(stake, MAX_STAKE)

    log(f"🎯 {SYMBOL} {direction} | STAKE {stake:.2f}")

    proposal = {
        "proposal": 1,
        "amount": stake,
        "basis": "stake",
        "contract_type": "CALL" if direction == "CALL" else "PUT",
        "currency": "USD",
        "duration": DURATION,
        "duration_unit": "t",
        "symbol": SYMBOL
    }

    ws.send(json.dumps(proposal))

# ================= WS HANDLERS =================
def on_open(socket):
    log("🌐 CONNECTED")
    socket.send(json.dumps({"authorize": API_TOKEN}))

def on_message(socket, message):
    global trade_in_progress, stake, current_contract

    data = json.loads(message)
    msg_type = data.get("msg_type")

    # AUTH
    if msg_type == "authorize":
        log("🔐 AUTHORIZED")
        return

    # PROPOSAL RESPONSE
    if msg_type == "proposal":
        if "error" in data:
            log(f"❌ PROPOSAL REJECTED: {data['error']['message']}")
            trade_in_progress = False
            return

        proposal = data.get("proposal")
        if not proposal or "id" not in proposal:
            log("⚠ INVALID PROPOSAL — SKIPPED")
            trade_in_progress = False
            return

        pid = proposal["id"]
        ws.send(json.dumps({"buy": pid, "price": proposal["ask_price"]}))
        log(f"🟢 BUY CONFIRMED {pid}")
        return

    # BUY CONFIRM
    if msg_type == "buy":
        current_contract = data["buy"]["contract_id"]
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": current_contract,
            "subscribe": 1
        }))
        return

    # CONTRACT UPDATE
    if msg_type == "proposal_open_contract":
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            profit = float(poc.get("profit", 0))

            if profit > 0:
                stake += profit
                log(f"✔ WIN {profit:.2f} → {stake:.2f}")
            else:
                log(f"❌ LOSS {profit:.2f} → RESET")
                stake = BASE_STAKE

            trade_in_progress = False
            current_contract = None
            return

def on_error(socket, error):
    log(f"❌ WS ERROR: {error}")

def on_close(socket):
    log("🔁 WS CLOSED — RECONNECTING")
    time.sleep(2)
    start_bot()

# ================= STRATEGY LOOP =================
def strategy_loop():
    while True:
        if can_trade():
            direction = "CALL" if int(time.time()) % 2 == 0 else "PUT"
            send_proposal(direction)
        time.sleep(0.2)

# ================= START =================
def start_bot():
    global ws
    ws = websocket.WebSocketApp(
        DERIV_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

if __name__ == "__main__":
    log("🚀 V10 CLOUD BOT — LIVE")
    start_bot()
    threading.Thread(target=strategy_loop, daemon=True).start()

    while True:
        time.sleep(1)