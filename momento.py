import json
import time
import threading
import websocket
import logging
import os
from collections import deque
from datetime import datetime

# ================= CONFIG =================
API_TOKEN = os.getenv("DERIV_TOKEN")  # set in Koyeb
APP_ID = os.getenv("APP_ID", "1089")
SYMBOL = "R_75"

MODE = "AGGRESSIVE"  # SAFE | AGGRESSIVE

BASE_STAKE = 1.0
MAX_STAKE = 50.0

TICK_WINDOW = 20
COOLDOWN = 5

# =========================================

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("MOMENTO")

# ---------- BOT ----------
class MomentoBot:
    def __init__(self):
        self.ws = None
        self.ticks = deque(maxlen=200)
        self.last_trade = 0
        self.balance = 0.0
        self.running = True

    # ---------- CONNECTION ----------
    def connect(self):
        url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
        self.ws = websocket.WebSocketApp(
            url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def on_open(self, ws):
        log.info("✅ Connected to Deriv WebSocket")
        self.send({"authorize": API_TOKEN})

    def on_close(self, ws, code, msg):
        log.warning("❌ WebSocket closed")

    def on_error(self, ws, error):
        log.error(f"WS ERROR: {error}")

    # ---------- MESSAGE HANDLER ----------
    def on_message(self, ws, message):
        try:
            data = json.loads(message)

            if "authorize" in data:
                log.info("🔐 Authorized")
                self.send({"balance": 1, "subscribe": 1})

            elif "balance" in data:
                self.balance = data["balance"]["balance"]

            elif "tick" in data:
                price = float(data["tick"]["quote"])
                self.ticks.append(price)
                log.info(f"TICK {price}")
                self.evaluate()

            elif "proposal" in data:
                pid = data["proposal"]["id"]
                self.send({"buy": pid, "price": BASE_STAKE})

            elif "buy" in data:
                log.info(f"🎯 TRADE EXECUTED | ID {data['buy']['contract_id']}")

        except Exception as e:
            log.error(f"on_message error: {e}")

    # ---------- SIGNAL ENGINE ----------
    def evaluate(self):
        if len(self.ticks) < TICK_WINDOW:
            return

        if time.time() - self.last_trade < COOLDOWN:
            return

        prices = list(self.ticks)[-TICK_WINDOW:]  # ✅ FIXED

        momentum = prices[-1] - prices[0]

        if MODE == "AGGRESSIVE":
            threshold = 2.0
        else:
            threshold = 4.0

        if momentum > threshold:
            self.trade("CALL")

        elif momentum < -threshold:
            self.trade("PUT")

        self.heartbeat()

    # ---------- TRADE ----------
    def trade(self, direction):
        if BASE_STAKE > MAX_STAKE:
            return

        log.info(f"🚀 SIGNAL {direction} | Stake ${BASE_STAKE}")
        self.last_trade = time.time()

        self.send({
            "proposal": 1,
            "amount": BASE_STAKE,
            "basis": "stake",
            "contract_type": direction,
            "currency": "USD",
            "duration": 1,
            "duration_unit": "t",
            "symbol": SYMBOL
        })

    # ---------- HEARTBEAT ----------
    def heartbeat(self):
        log.info(f"❤️ HEARTBEAT | {MODE} | Balance ${self.balance:.2f}")

    # ---------- SEND ----------
    def send(self, payload):
        if self.ws:
            self.ws.send(json.dumps(payload))


# ---------- RUN ----------
if __name__ == "__main__":
    log.info("🔥 MOMENTO BOT STARTING")
    bot = MomentoBot()
    bot.connect()