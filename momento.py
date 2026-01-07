import os, json, threading, time, signal
from collections import deque
import websocket
import pandas as pd
import numpy as np

# -------------------------
# CONFIG
# -------------------------
DERIV_TOKEN = os.getenv("DERIV_API_TOKEN")  # Must be set in Koyeb environment
APP_ID = int(os.getenv("APP_ID", 1089))
SYMBOL = "R_75"
INITIAL_BALANCE = 100.0
RISK_PER_TRADE = 0.02
MAX_OPEN_TRADES = 2
MAX_CONSECUTIVE_LOSSES = 3
CCI_SHORT = 14
AGGRESSION = "MEDIUM"
ENTRY_COOLDOWN = 30
SLIPPAGE = 0.0001

# -------------------------
# STATE
# -------------------------
balance = INITIAL_BALANCE
wins = 0
losses = 0
consecutive_losses = 0
open_trades = []  # track live trade IDs
last_entry_time = 0
shutdown_event = threading.Event()
alerts = deque(maxlen=20)
ticks = deque(maxlen=2000)
symbol_last_entry = {}
trade_in_progress = False
TRADE_AMOUNT = 0.0
ws = None
lock = threading.Lock()

# -------------------------
# SIGNAL HANDLER
# -------------------------
def append_alert(msg):
    with lock:
        alerts.appendleft(f"{time.strftime('%H:%M:%S')} {msg}")
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def handle_sigterm(signum, frame):
    append_alert("Shutdown signal received — exiting...")
    shutdown_event.set()

signal.signal(signal.SIGINT, handle_sigterm)
signal.signal(signal.SIGTERM, handle_sigterm)

# -------------------------
# INDICATORS
# -------------------------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def atr_value(df, period=14):
    if len(df) < period + 2:
        return max(0.0005, float(df['Close'].pct_change().rolling(5).std().iloc[-1] or 0.001))
    rng = df['Close'].rolling(period).max() - df['Close'].rolling(period).min()
    val = float(rng.iloc[-1]) if not rng.empty and not np.isnan(rng.iloc[-1]) else 0.001
    return max(0.0005, val)

def cci(df, period):
    tp = df['Close']
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    c = (tp - ma) / (0.015 * md)
    c.fillna(0, inplace=True)
    return c

def compute_adx(df, period=14):
    if len(df) < period + 2: return 25.0
    h = df['Close'].rolling(2).max()
    l = df['Close'].rolling(2).min()
    tr = (h - l).abs()
    tr14 = tr.ewm(alpha=1/period, adjust=False).mean()
    plus = df['Close'].diff().clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    minus = (-df['Close'].diff()).clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus / tr14)
    minus_di = 100 * (minus / tr14)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
    adx = dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
    return float(adx if not np.isnan(adx) else 25.0)

def aggression_params(agg):
    if agg == "LOW":
        return {"cci_short": 90, "atr_mult_tp": 2.5, "atr_mult_sl": 1.0, "pullback_pct": 0.002}
    if agg == "HIGH":
        return {"cci_short": 40, "atr_mult_tp": 0.9, "atr_mult_sl": 0.6, "pullback_pct": 0.01}
    return {"cci_short": 60, "atr_mult_tp": 1.5, "atr_mult_sl": 0.8, "pullback_pct": 0.005}

AGG = aggression_params(AGGRESSION)

# -------------------------
# Koyeb-ready WebSocket
# -------------------------
mode_live = False
authorized = False

def send_authorize():
    global ws
    ws.send(json.dumps({"authorize": DERIV_TOKEN, "app_id": APP_ID}))

def on_message(ws, msg):
    global balance, trade_in_progress, authorized, wins, losses, consecutive_losses
    try:
        data = json.loads(msg)
        # Authorization confirmation
        if "authorize" in data:
            authorized = True
            append_alert("✔ Authorized")
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            ws.send(json.dumps({"balance": 1, "subscribe": 1}))
            ws.send(json.dumps({"proposal_open_contract": 1, "subscribe": 1}))

        # Ticks
        if "tick" in data:
            tick = float(data["tick"]["quote"])
            ticks.append(tick)

        # Balance update
        if "balance" in data:
            balance = float(data["balance"]["balance"])

        # Contract settlements
        if "proposal_open_contract" in data:
            for c in data["proposal_open_contract"]:
                if c.get("is_sold") or c.get("is_expired"):
                    profit = float(c.get("profit") or 0)
                    balance += profit
                    if profit > 0:
                        wins += 1
                        consecutive_losses = 0
                    else:
                        losses += 1
                        consecutive_losses += 1
                    trade_in_progress = False
                    append_alert(f"✔ Settlement: Profit={profit:.2f} | Balance={balance:.2f}")
    except Exception as e:
        append_alert(f"WS parse error: {e}")

def on_error(ws, error):
    append_alert(f"❌ WS Error: {error}")

def on_close(ws, *_):
    append_alert("❌ WS Closed, reconnecting in 2s")
    time.sleep(2)
    start_ws()

def start_ws():
    global ws, mode_live, authorized
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
        on_open=lambda ws: send_authorize(),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()
    mode_live = True

# -------------------------
# TRADE ENGINE
# -------------------------
def can_enter_now():
    now = time.time()
    last = symbol_last_entry.get(SYMBOL, 0)
    return now - last > ENTRY_COOLDOWN

def place_trade(direction, stake):
    global trade_in_progress, TRADE_AMOUNT
    if not authorized or trade_in_progress or len(open_trades) >= MAX_OPEN_TRADES:
        return
    TRADE_AMOUNT = stake
    ct = "CALL" if direction == "BULL" else "PUT"
    proposal = {
        "proposal": 1,
        "amount": TRADE_AMOUNT,
        "basis": "stake",
        "contract_type": ct,
        "currency": "USD",
        "duration": 1,
        "duration_unit": "t",
        "symbol": SYMBOL
    }
    try:
        ws.send(json.dumps(proposal))
        trade_in_progress = True
        symbol_last_entry[SYMBOL] = time.time()
        append_alert(f"📈 Trade sent → {direction} | Stake={TRADE_AMOUNT:.2f}")
    except Exception as e:
        append_alert(f"⚠ Trade send failed: {e}")

def trade_engine():
    global balance, wins, losses, consecutive_losses
    while not shutdown_event.is_set():
        if len(ticks) < 25 or not authorized:
            time.sleep(0.5)
            continue

        prices = pd.Series(list(ticks))
        df = pd.DataFrame({"Close": prices})
        df["EMA50"] = ema(df["Close"], 50)
        df["EMA200"] = ema(df["Close"], 200)
        df["CCI14"] = cci(df, CCI_SHORT)
        adx_val = compute_adx(df)

        last_close = df["Close"].iloc[-1]
        ema200 = df["EMA200"].iloc[-1]
        ema50 = df["EMA50"].iloc[-1]
        cci_short = df["CCI14"].iloc[-1]
        atr = atr_value(df, period=14)

        trend = "BULL" if last_close > ema200 else ("BEAR" if last_close < ema200 else None)
        params = AGG
        cci_threshold = params["cci_short"]

        if trend and can_enter_now():
            stake = round(balance * RISK_PER_TRADE, 2)
            if trend == "BULL" and cci_short > cci_threshold:
                place_trade("BULL", stake)
            elif trend == "BEAR" and cci_short < -cci_threshold:
                place_trade("BEAR", stake)

        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            append_alert("Max consecutive losses reached — stopping trading")
            shutdown_event.set()

        time.sleep(0.5)

# -------------------------
# START BOT
# -------------------------
start_ws()
threading.Thread(target=trade_engine, daemon=True).start()
append_alert("✅ Live Koyeb-ready EMA bot started")

while not shutdown_event.is_set():
    time.sleep(1)