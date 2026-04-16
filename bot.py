"""
GSE Swing Trading Signal Bot (GitHub Actions Version)
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ── Configuration ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

GSE_LIVE_URL = "https://dev.kwayisi.org/apis/gse/live"
WATCHLIST = {"GCB", "MTNGH", "EGL", "GOIL", "SCB", "PBC", "CPC"}
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "100"))
MAX_RETRIES = 3
GHANA_TZ = timezone(timedelta(hours=0))
ALERT_COOLDOWN_MINUTES = int(os.environ.get("ALERT_COOLDOWN_MINUTES", "120"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "200"))

# Indicator periods
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21
VOLATILITY_PERIOD = 14
MIN_HISTORY = 25

# Risk/Reward
RR_RATIO = 2.0
ATR_SL_MULT = 1.5

# RSI thresholds
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "history.json"
VOLUME_FILE = BASE_DIR / "volume.json"
ALERT_STATE_FILE = BASE_DIR / "alert_state.json"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
price_history = defaultdict(list)
volume_history = defaultdict(list)
alert_state = defaultdict(dict)

# ── Time ──────────────────────────────────────────────────────────────────────
def ghana_now() -> datetime:
    return datetime.now(tz=GHANA_TZ)

def market_open() -> bool:
    now = ghana_now()
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    return start <= now <= end

# ── Persistence ───────────────────────────────────────────────────────────────
def load_history():
    global price_history, volume_history, alert_state
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            price_history = defaultdict(list, json.load(f))
        with VOLUME_FILE.open("r", encoding="utf-8") as f:
            volume_history = defaultdict(list, json.load(f))
        if ALERT_STATE_FILE.exists():
            with ALERT_STATE_FILE.open("r", encoding="utf-8") as f:
                alert_state = defaultdict(dict, json.load(f))
        log.info("Successfully loaded price and volume history.")
    except FileNotFoundError:
        log.warning("History files not found. Starting fresh.")
    except json.JSONDecodeError:
        log.error("Error decoding history files. Starting fresh.")

def save_history():
    with HISTORY_FILE.open("w", encoding="utf-8") as f:
        json.dump(price_history, f)
    with VOLUME_FILE.open("w", encoding="utf-8") as f:
        json.dump(volume_history, f)
    with ALERT_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(alert_state, f)
    log.info("Successfully saved price and volume history.")

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram_msg(message: str, retries: int = MAX_RETRIES):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN or CHAT_ID not set. Cannot send Telegram message.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    for attempt in range(retries):
        try:
            r = requests.post(url, data=payload, timeout=10)
            if r.status_code == 200:
                return
            log.error("Telegram error (attempt %d): %s", attempt + 1, r.text)
        except Exception as e:
            log.error("Telegram failed (attempt %d): %s", attempt + 1, e)
        if attempt < retries - 1:
            time.sleep(2)

# ── GSE API ───────────────────────────────────────────────────────────────────
def get_live_gse_data() -> list:
    try:
        r = requests.get(GSE_LIVE_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log.error("API response is not a list: %s", data)
            return []
        return data
    except requests.exceptions.RequestException as e:
        log.error("Failed to fetch GSE data: %s", e)
    except json.JSONDecodeError as e:
        log.error("Failed to decode GSE API response: %s", e)
    return []

# ── Indicators ────────────────────────────────────────────────────────────────
def calc_rsi(prices: list, period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 1:
        return 50.0
    series = pd.Series(prices)
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calc_ema(prices: list, period: int) -> float:
    if not prices:
        return 0.0
    return pd.Series(prices).ewm(span=period, adjust=False).mean().iloc[-1]

def calc_volatility(prices: list, period: int = VOLATILITY_PERIOD) -> float:
    if len(prices) < 2:
        return prices[-1] * 0.02 if prices else 0.0

    returns = pd.Series(prices).diff().abs().dropna()
    if len(returns) < period:
        return returns.mean() if not returns.empty else 0.0
    return returns.tail(period).mean()

def has_fresh_tick(ticker: str, price: float, volume: int) -> bool:
    ph = price_history[ticker]
    vh = volume_history[ticker]
    if not ph or not vh:
        return True
    return price != ph[-1] or volume != vh[-1]

def update_history(ticker: str, price: float, volume: int):
    price_history[ticker].append(price)
    volume_history[ticker].append(volume)
    if len(price_history[ticker]) > MAX_HISTORY:
        price_history[ticker] = price_history[ticker][-MAX_HISTORY:]
    if len(volume_history[ticker]) > MAX_HISTORY:
        volume_history[ticker] = volume_history[ticker][-MAX_HISTORY:]

def should_send_signal(ticker: str, sig: dict) -> bool:
    state = alert_state[ticker]
    now = ghana_now()
    last_signal = state.get("signal")
    last_sent_raw = state.get("sent_at")

    if not last_signal or not last_sent_raw:
        return True

    try:
        last_sent = datetime.fromisoformat(last_sent_raw)
    except ValueError:
        return True

    cooldown = timedelta(minutes=ALERT_COOLDOWN_MINUTES)
    return not (last_signal == sig["signal"] and now - last_sent < cooldown)

def record_signal(ticker: str, sig: dict):
    alert_state[ticker] = {
        "signal": sig["signal"],
        "sent_at": ghana_now().isoformat(),
        "entry": sig["entry"],
    }


# ── Signal logic ──────────────────────────────────────────────────────────────
def generate_signal(ticker: str, price: float, volume: int) -> dict | None:
    ph = price_history[ticker]
    vh = volume_history[ticker]

    if len(ph) < MIN_HISTORY:
        return None

    rsi = calc_rsi(ph)
    volatility = calc_volatility(ph)
    if not vh:
        avg_vol = volume
    else:
        avg_vol = np.mean(vh[-14:])

    if avg_vol == 0:
        vol_ok = volume > 0
    else:
        vol_ok = volume >= avg_vol * 1.2

    ema9 = calc_ema(ph, EMA_FAST)
    ema21 = calc_ema(ph, EMA_SLOW)
    prev_ema9 = calc_ema(ph[:-1], EMA_FAST)
    prev_ema21 = calc_ema(ph[:-1], EMA_SLOW)

    if (
        ema9 is None
        or ema21 is None
        or prev_ema9 is None
        or prev_ema21 is None
        or pd.isna(rsi)
        or pd.isna(volatility)
        or volatility <= 0
        or volume < MIN_VOLUME
    ):
        return None

    bullish_cross = prev_ema9 <= prev_ema21 and ema9 > ema21
    bearish_cross = prev_ema9 >= prev_ema21 and ema9 < ema21

    if rsi < RSI_OVERSOLD and bullish_cross and vol_ok:
        sl = round(max(price - ATR_SL_MULT * volatility, 0.0001), 4)
        tp = round(price + ATR_SL_MULT * volatility * RR_RATIO, 4)
        return {
            "signal": "BUY",
            "entry": price,
            "sl": sl,
            "tp": tp,
            "rsi": rsi,
            "ema9": ema9,
            "ema21": ema21,
            "volatility": volatility,
            "vol_ratio": (volume / avg_vol if avg_vol > 0 else 1),
        }

    if rsi > RSI_OVERBOUGHT and bearish_cross and vol_ok:
        sl = round(price + ATR_SL_MULT * volatility, 4)
        tp = round(max(price - ATR_SL_MULT * volatility * RR_RATIO, 0.0001), 4)
        return {
            "signal": "SELL",
            "entry": price,
            "sl": sl,
            "tp": tp,
            "rsi": rsi,
            "ema9": ema9,
            "ema21": ema21,
            "volatility": volatility,
            "vol_ratio": (volume / avg_vol if avg_vol > 0 else 1),
        }

    return None

# ── Message builder ───────────────────────────────────────────────────────────
SIGNAL_EMOJI = {"BUY": "🟢", "SELL": "🔴"}
def build_signal_msg(ticker: str, sig: dict) -> str:
    s = sig["signal"]
    wl_tag = " ⭐" if ticker in WATCHLIST else ""
    rr_label = f"{RR_RATIO:.0f}:1 Risk/Reward"
    sl_pct = abs(sig["entry"] - sig["sl"]) / sig["entry"] * 100 if sig["entry"] > 0 else 0
    tp_pct = abs(sig["tp"] - sig["entry"]) / sig["entry"] * 100 if sig["entry"] > 0 else 0

    return (
        f"{SIGNAL_EMOJI[s]} *{s} SIGNAL — {ticker}*{wl_tag}
"
        f"{'─' * 30}
"
        f"📌 Entry:       *GHS {sig['entry']:.4f}*
"
        f"🛑 Stop Loss:   *GHS {sig['sl']:.4f}*  (-{sl_pct:.2f}%)
"
        f"🎯 Take Profit: *GHS {sig['tp']:.4f}*  (+{tp_pct:.2f}%)
"
        f"{'─' * 30}
"
        f"📊 RSI-14:  {sig['rsi']:.1f}
"
        f"📈 EMA-9:   GHS {sig['ema9']:.4f}
"
        f"📉 EMA-21:  GHS {sig['ema21']:.4f}
"
        f"📏 Volatility: GHS {sig['volatility']:.4f}
"
        f"💧 Volume:  {sig['vol_ratio']:.1f}× average
"
        f"{'─' * 30}
"
        f"⚖️ {rr_label}  |  Swing trade
"
        f"⚠️ _Always manage your risk._"
    )

# ── Core scan ─────────────────────────────────────────────────────────────────
def check_market():
    log.info("Scanning GSE live data...")
    data = get_live_gse_data()

    if not data:
        log.warning("No data returned from GSE API.")
        return

    processed_tickers = set()
    for item in data:
        ticker = item.get("name")
        try:
            price = float(item.get("price", 0.0))
            volume = int(item.get("volume", 0))
        except (ValueError, TypeError):
            log.warning("Invalid price/volume for %s. Skipping.", ticker)
            continue

        if not ticker or price <= 0:
            continue

        if ticker not in WATCHLIST:
            continue

        if ticker in processed_tickers:
            continue
        processed_tickers.add(ticker)

        if has_fresh_tick(ticker, price, volume):
            update_history(ticker, price, volume)

        sig = generate_signal(ticker, price, volume)

        if sig:
            if not should_send_signal(ticker, sig):
                log.info("Cooldown active for %s %s signal.", ticker, sig["signal"])
                continue
            msg = build_signal_msg(ticker, sig)
            send_telegram_msg(msg)
            record_signal(ticker, sig)
            log.info("Signal [%s] → %s", sig["signal"], ticker)
        else:
            remaining = MIN_HISTORY - len(price_history[ticker])
            if remaining > 0 and ticker in WATCHLIST:
                log.info("%s warming up — %d more polls needed.", ticker, remaining)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if not market_open():
        log.info("Market is closed. Exiting.")
        return

    if not BOT_TOKEN or not CHAT_ID:
        log.critical("BOT_TOKEN and CHAT_ID environment variables must be set.")
        sys.exit(1)

    load_history()
    check_market()
    save_history()
    log.info("Bot run finished.")

if __name__ == "__main__":
    main()
