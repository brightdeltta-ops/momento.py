import json, threading, time, websocket, numpy as np, os
from collections import deque
from rich.console import Console
from rich.text import Text
from datetime import datetime

# ================= ENV CONFIG =================
API_TOKEN = os.getenv("DERIV_API_TOKEN")
APP_ID = int(os.getenv("APP_ID", "0"))

if not API_TOKEN or not APP_ID:
    raise RuntimeError("Missing DERIV_API_TOKEN or APP_ID environment variables")

# ================= TRADING CONFIG =================
SYMBOL = "R_75"

BASE_RISK = 0.012            # 1.2% normal risk
BURST_RISK = 0.015           # 1.5% burst risk
MAX_STAKE = 100.0
MAX_DAILY_DD = 0.06          # 6% daily kill switch
COOLDOWN = 6
DURATION = 1                # ticks

EMA_FAST = 3
EMA_SLOW = 10
VOL_WINDOW = 20
VOL_THRESHOLD = 0.0015

# ================= STATE =================
tick_buffer = deque(maxlen=50)
price_history = deque(maxlen=500)

BALANCE = 0.0
DAY_START_BAL = 0.0
MAX_BALANCE = 0.0

WINS = 0
LOSSES = 0
TRADE_COUNT = 0

trade_in_progress = False
last_trade_time = 0
last_direction = None

win_streak = 0
burst_mode = False

ws = None
console = Console()

# ================= INDICATORS =================
def ema(data, p):
    if len(data) < p:
        return None
    w = np.exp(np.linspace(-1, 0, p))
    w /= w.sum()
    return np.convolve(data[-p:], w, mode="valid")[0]

# ================= RISK =================
def daily_dd_exceeded():
    if DAY_START_BAL == 0:
        return False
    dd = (DAY_START_BAL - BALANCE) / DAY_START_BAL
    return dd >= MAX_DAILY_DD

def calc_stake(risk):
    stake = BALANCE * risk
    return min(stake, MAX_STAKE)

# ================= LOGGING =================
def log(msg, color="white"):
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[{color}][{ts}] {msg}[/{color}]")

# ================= STRATEGY =================
def evaluate_trade():
    global burst_mode, last_trade_time

    if trade_in_progress:
        return
    if time.time() - last_trade_time < COOLDOWN:
        return
    if daily_dd_exceeded():
        log("⛔ DAILY LOSS LIMIT HIT — BOT STOPPED", "red")
        return

    if len(price_history) < VOL_WINDOW:
        return

    vol = np.std(list(price_history)[-VOL_WINDOW:])
    if vol < VOL_THRESHOLD:
        return

    f = ema(price_history, EMA_FAST)
    s = ema(price_history, EMA_SLOW)
    if f is None or s is None:
        return

    direction = "up" if f > s else "down"
    if direction == last_direction:
        return

    risk = BURST_RISK if burst_mode else BASE_RISK
    stake = calc_stake(risk)

    send_trade(direction, stake)
    last_trade_time = time.time()

# ================= TRADE EXEC =================
def send_trade(direction, stake):
    global trade_in_progress, last_direction

    ct = "CALL" if direction == "up" else "PUT"
    ws.send(json.dumps({
        "proposal": 1,
        "amount": round(stake, 2),
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": DURATION,
        "duration_unit": "t",
        "symbol": SYMBOL
    }))

    trade_in_progress = True
    last_direction = direction
    log(f"📤 PROPOSAL {ct} | Stake ${stake:.2f}", "magenta")

# ================= CONTRACT RESULT =================
def settle_trade(contract):
    global BALANCE, MAX_BALANCE, WINS, LOSSES, TRADE_COUNT
    global trade_in_progress, win_streak, burst_mode

    profit = float(contract.get("profit", 0))
    BALANCE += profit
    MAX_BALANCE = max(MAX_BALANCE, BALANCE)
    TRADE_COUNT += 1
    trade_in_progress = False

    if profit > 0:
        WINS += 1
        win_streak += 1
        log(f"✅ WIN +${profit:.2f} | Bal ${BALANCE:.2f}", "green")
    else:
        LOSSES += 1
        win_streak = 0
        burst_mode = False
        log(f"❌ LOSS ${profit:.2f} | Bal ${BALANCE:.2f}", "red")

    # BURST LOGIC
    if win_streak >= 2 and not burst_mode:
        burst_mode = True
        log("🔥 BURST MODE ACTIVATED", "yellow")
    if win_streak >= 3:
        burst_mode = False
        win_streak = 0
        log("🔁 BURST MODE RESET", "cyan")

# ================= WEBSOCKET =================
def start_ws():
    global ws, DAY_START_BAL

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log(f"Connecting to {url}", "yellow")

    def on_open(w):
        w.send(json.dumps({"authorize": API_TOKEN}))

    def on_message(w, msg):
        data = json.loads(msg)

        if "authorize" in data:
            log("✅ AUTHORIZED", "green")
            w.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            w.send(json.dumps({"balance": 1, "subscribe": 1}))
            w.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

        if "tick" in data:
            price = float(data["tick"]["quote"])
            price_history.append(price)
            log(f"TICK {price:.4f}", "cyan")
            evaluate_trade()

        if "proposal" in data:
            w.send(json.dumps({"buy": data["proposal"]["id"], "price": data["proposal"]["ask_price"]}))

        if "proposal_open_contract" in data:
            c = data["proposal_open_contract"]
            if c.get("is_sold"):
                settle_trade(c)

        if "balance" in data:
            global BALANCE
            BALANCE = float(data["balance"]["balance"])
            if DAY_START_BAL == 0:
                DAY_START_BAL = BALANCE

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=lambda w,e: log(f"WS ERROR {e}", "red"),
        on_close=lambda w,c,m: log("WS CLOSED", "red")
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= MAIN =================
if __name__ == "__main__":
    log("🚀 MOMENTO BOT — CAC MODE STARTED", "green")
    start_ws()
    while True:
        time.sleep(5)