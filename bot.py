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
HMM_MIN_HISTORY = int(os.environ.get("HMM_MIN_HISTORY", "30"))
HMM_CONFIDENCE_THRESHOLD = float(os.environ.get("HMM_CONFIDENCE_THRESHOLD", "0.60"))
HMM_FIT_ITERATIONS = int(os.environ.get("HMM_FIT_ITERATIONS", "6"))

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

def calc_returns(prices: list[float]) -> np.ndarray:
    if len(prices) < 2:
        return np.array([], dtype=float)
    series = pd.Series(prices, dtype=float)
    returns = series.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    return returns.to_numpy(dtype=float)

def gaussian_log_pdf(x: float, mean: float, std: float) -> float:
    variance = max(std ** 2, 1e-8)
    return -0.5 * np.log(2 * np.pi * variance) - ((x - mean) ** 2) / (2 * variance)

def viterbi_path(
    obs: np.ndarray,
    init_probs: np.ndarray,
    trans: np.ndarray,
    means: np.ndarray,
    stds: np.ndarray,
) -> np.ndarray:
    n_obs = len(obs)
    n_states = len(init_probs)
    log_init = np.log(np.clip(init_probs, 1e-8, 1.0))
    log_trans = np.log(np.clip(trans, 1e-8, 1.0))
    dp = np.full((n_obs, n_states), -np.inf, dtype=float)
    back = np.zeros((n_obs, n_states), dtype=int)

    for state in range(n_states):
        dp[0, state] = log_init[state] + gaussian_log_pdf(obs[0], means[state], stds[state])

    for idx in range(1, n_obs):
        for state in range(n_states):
            candidates = dp[idx - 1] + log_trans[:, state]
            best_prev = int(np.argmax(candidates))
            dp[idx, state] = candidates[best_prev] + gaussian_log_pdf(obs[idx], means[state], stds[state])
            back[idx, state] = best_prev

    path = np.zeros(n_obs, dtype=int)
    path[-1] = int(np.argmax(dp[-1]))
    for idx in range(n_obs - 2, -1, -1):
        path[idx] = back[idx + 1, path[idx + 1]]
    return path

def forward_state_probs(
    obs: np.ndarray,
    init_probs: np.ndarray,
    trans: np.ndarray,
    means: np.ndarray,
    stds: np.ndarray,
) -> np.ndarray:
    alpha = np.clip(init_probs, 1e-8, 1.0).astype(float)
    alpha = alpha / alpha.sum()

    for idx, value in enumerate(obs):
        emission = np.array(
            [np.exp(gaussian_log_pdf(value, means[state], stds[state])) for state in range(len(alpha))],
            dtype=float,
        )
        if idx == 0:
            alpha = alpha * emission
        else:
            alpha = (alpha @ trans) * emission
        alpha = alpha / max(alpha.sum(), 1e-8)
    return alpha

def infer_hmm_regime(prices: list[float]) -> dict | None:
    returns = calc_returns(prices)
    if len(returns) < HMM_MIN_HISTORY:
        return None

    median = float(np.median(returns))
    states = np.where(returns >= median, 1, 0)
    if np.all(states == states[0]):
        states[-1] = 1 - states[-1]

    overall_std = max(float(np.std(returns)), 1e-4)
    init_probs = np.array([0.5, 0.5], dtype=float)
    trans = np.array([[0.5, 0.5], [0.5, 0.5]], dtype=float)
    means = np.array([float(np.mean(returns)), float(np.mean(returns))], dtype=float)
    stds = np.array([overall_std, overall_std], dtype=float)

    for _ in range(HMM_FIT_ITERATIONS):
        for state in range(2):
            state_obs = returns[states == state]
            if len(state_obs) == 0:
                means[state] = float(np.mean(returns))
                stds[state] = overall_std
            else:
                means[state] = float(np.mean(state_obs))
                stds[state] = max(float(np.std(state_obs)), overall_std * 0.5, 1e-4)

        init_counts = np.ones(2, dtype=float)
        init_counts[states[0]] += 1.0
        init_probs = init_counts / init_counts.sum()

        trans_counts = np.ones((2, 2), dtype=float)
        for prev_state, next_state in zip(states[:-1], states[1:]):
            trans_counts[prev_state, next_state] += 1.0
        trans = trans_counts / trans_counts.sum(axis=1, keepdims=True)

        new_states = viterbi_path(returns, init_probs, trans, means, stds)
        if np.array_equal(new_states, states):
            states = new_states
            break
        states = new_states

    bullish_state = int(np.argmax(means))
    bearish_state = int(np.argmin(means))
    posterior = forward_state_probs(returns, init_probs, trans, means, stds)
    current_state = int(np.argmax(posterior))

    return {
        "state": "BULL" if current_state == bullish_state else "BEAR",
        "confidence": float(posterior[current_state]),
        "bull_prob": float(posterior[bullish_state]),
        "bear_prob": float(posterior[bearish_state]),
    }

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
    hmm_regime = infer_hmm_regime(ph)
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
        or hmm_regime is None
    ):
        return None

    bullish_cross = prev_ema9 <= prev_ema21 and ema9 > ema21
    bearish_cross = prev_ema9 >= prev_ema21 and ema9 < ema21
    hmm_bull_ok = hmm_regime["state"] == "BULL" and hmm_regime["bull_prob"] >= HMM_CONFIDENCE_THRESHOLD
    hmm_bear_ok = hmm_regime["state"] == "BEAR" and hmm_regime["bear_prob"] >= HMM_CONFIDENCE_THRESHOLD

    if rsi < RSI_OVERSOLD and bullish_cross and vol_ok and hmm_bull_ok:
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
            "hmm_state": hmm_regime["state"],
            "hmm_confidence": hmm_regime["confidence"],
            "hmm_bull_prob": hmm_regime["bull_prob"],
            "hmm_bear_prob": hmm_regime["bear_prob"],
        }

    if rsi > RSI_OVERBOUGHT and bearish_cross and vol_ok and hmm_bear_ok:
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
            "hmm_state": hmm_regime["state"],
            "hmm_confidence": hmm_regime["confidence"],
            "hmm_bull_prob": hmm_regime["bull_prob"],
            "hmm_bear_prob": hmm_regime["bear_prob"],
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
        f"{SIGNAL_EMOJI[s]} *{s} SIGNAL — {ticker}*{wl_tag}\n"
        f"{'─' * 30}\n"
        f"📌 Entry:       *GHS {sig['entry']:.4f}*\n"
        f"🛑 Stop Loss:   *GHS {sig['sl']:.4f}*  (-{sl_pct:.2f}%)\n"
        f"🎯 Take Profit: *GHS {sig['tp']:.4f}*  (+{tp_pct:.2f}%)\n"
        f"{'─' * 30}\n"
        f"📊 RSI-14:  {sig['rsi']:.1f}\n"
        f"📈 EMA-9:   GHS {sig['ema9']:.4f}\n"
        f"📉 EMA-21:  GHS {sig['ema21']:.4f}\n"
        f"📏 Volatility: GHS {sig['volatility']:.4f}\n"
        f"💧 Volume:  {sig['vol_ratio']:.1f}× average\n"
        f"🧠 HMM Regime: {sig['hmm_state']} ({sig['hmm_confidence'] * 100:.0f}% confidence)\n"
        f"🟢 Bull Prob: {sig['hmm_bull_prob'] * 100:.0f}% | 🔴 Bear Prob: {sig['hmm_bear_prob'] * 100:.0f}%\n"
        f"{'─' * 30}\n"
        f"⚖️ {rr_label}  |  Swing trade\n"
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
