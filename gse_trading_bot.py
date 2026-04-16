"""
Dual-Market Trading Bot — Ghana Stock Exchange (GSE) + US Markets
Designed for ephemeral GitHub Actions runs with JSON state persistence.

Per-run lifecycle:
  1. Load state from bot_state.json (persisted via actions/cache)
  2. Validate Telegram
  3. Scan any open markets
  4. Send EOD summary for markets that just closed
  5. Save updated state → bot_state.json
  6. Exit
  
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests
from pathlib import Path

# Load .env if present
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if os.getenv(k) is None:
                os.environ[k] = v

# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dualbot")

# ══════════════════════════════════════════════════════════════════════════════
# Configuration — all secrets via env-vars, fallbacks for local dev only
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID:   str = os.getenv("TG_CHAT_ID",   "YOUR_CHAT_ID_HERE")
DEBUG_MODE: bool = os.getenv("BOT_DEBUG", "0") == "1"
FORCE_SCAN: bool = os.getenv("FORCE_SCAN", "no").lower() == "yes"
STATE_FILE: Path = Path(os.getenv("STATE_FILE", "bot_state.json"))
STATE_VERSION = 3

# US tickers to monitor — extend freely
US_WATCHLIST: list[str] = [
    # Mega-cap
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    # Finance
    "JPM", "GS", "BAC", "V", "MA",
    # Energy
    "XOM", "CVX",
    # Healthcare
    "JNJ", "UNH",
    # ETFs
    "SPY", "QQQ", "IWM", "GLD", "SLV",
    # High-beta / growth
    "AMD", "PLTR", "SHOP", "COIN", "MSTR",
]

# ══════════════════════════════════════════════════════════════════════════════
# Enumerations
# ══════════════════════════════════════════════════════════════════════════════

class MarketID(Enum):
    GSE = auto()
    US  = auto()


class SignalType(Enum):
    BREAKOUT     = "Breakout"
    ACCUMULATION = "Accumulation"
    MOMENTUM     = "Momentum"
    REVERSAL     = "Reversal"
    NONE         = "None"


# ══════════════════════════════════════════════════════════════════════════════
# Shared HTTP session
# ══════════════════════════════════════════════════════════════════════════════

_session = requests.Session()
_session.headers.update({"User-Agent": "DualMarketBot/3.0"})

# ══════════════════════════════════════════════════════════════════════════════
# Market definitions
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ScoringWeights:
    price_w:    float = 0.6
    volume_w:   float = 2.0
    momentum_w: float = 1.0
    rsi_w:      float = 0.8
    threshold:  float = 4.0


@dataclass(frozen=True)
class MarketConfig:
    id:           MarketID
    label:        str
    flag:         str          # emoji flag
    currency:     str
    tz:           ZoneInfo
    open_h:       int
    open_m:       int
    close_h:      int
    close_m:      int
    api_url:      str
    weights:      ScoringWeights
    min_price:    float = 0.01
    min_volume:   float = 100.0
    api_timeout:  int   = 12
    api_retries:  int   = 3
    extra_headers: dict[str, str] = field(default_factory=dict)


GSE_CONFIG = MarketConfig(
    id        = MarketID.GSE,
    label     = "Ghana Stock Exchange",
    flag      = "🇬🇭",
    currency  = "GHS",
    tz        = ZoneInfo("Africa/Accra"),   # UTC+0 year-round
    open_h    = 9,  open_m  = 30,
    close_h   = 15, close_m = 0,
    api_url   = "https://dev.kwayisi.org/apis/gse/live",
    weights   = ScoringWeights(
        price_w    = 0.7,
        volume_w   = 1.5,
        momentum_w = 0.8,
        rsi_w      = 0.5,
        threshold  = 3.5,
    ),
    min_price  = 0.01,
    min_volume = 50.0,
)

US_CONFIG = MarketConfig(
    id        = MarketID.US,
    label     = "US Markets (NYSE / NASDAQ)",
    flag      = "🇺🇸",
    currency  = "USD",
    tz        = ZoneInfo("America/New_York"),
    open_h    = 9,  open_m  = 30,
    close_h   = 16, close_m = 0,
    api_url   = "https://query1.finance.yahoo.com/v8/finance/spark",
    weights   = ScoringWeights(
        price_w    = 0.6,
        volume_w   = 2.0,
        momentum_w = 1.2,
        rsi_w      = 1.0,
        threshold  = 4.5,
    ),
    min_price   = 1.00,
    min_volume  = 10_000.0,
    api_timeout = 15,
    api_retries = 4,
    extra_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    },
)

ALL_MARKETS: list[MarketConfig] = [GSE_CONFIG, US_CONFIG]

# ══════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ══════════════════════════════════════════════════════════════════════════════

def to_float(value: Any, *, default: float | None = None) -> float | None:
    """Safely coerce any API value to float."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def pct_change(new: float, old: float) -> float:
    return ((new - old) / old) * 100.0 if old and old > 0 else 0.0


def log2_safe(x: float) -> float:
    """log2(x) floored at 0 — below-average volume is neutral, not negative."""
    return max(math.log2(x), 0.0) if x > 0 else 0.0


def chunks(lst: list, n: int) -> Iterator[list]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ══════════════════════════════════════════════════════════════════════════════
# Scoring engine
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScoreResult:
    score:        float
    price_change: float
    volume_ratio: float
    momentum:     float
    rsi:          float
    signal:       SignalType
    above_thresh: bool


def _compute_rsi(prices: list[float], period: int = 14) -> float:
    """Wilder RSI; returns NaN when fewer than period+1 points available."""
    if not prices or len(prices) < period + 1:
        return math.nan

    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def _classify_signal(
    price_change: float,
    volume_ratio: float,
    momentum:     float,
    rsi:          float,
) -> SignalType:
    r = 50.0 if math.isnan(rsi) else rsi

    if price_change >= 3.0 and volume_ratio >= 2.0:
        return SignalType.BREAKOUT
    if price_change >= 1.0 and volume_ratio >= 3.0:
        return SignalType.ACCUMULATION
    if price_change >= 2.0 and momentum >= 0.6 and r < 70:
        return SignalType.MOMENTUM
    if price_change >= 1.0 and r < 35 and volume_ratio >= 1.5:
        return SignalType.REVERSAL
    return SignalType.NONE


def score_stock(
    current_price:  float,
    current_volume: float,
    last_price:     float,
    last_volume:    float,
    weights:        ScoringWeights,
    price_history:  list[float] | None = None,
) -> ScoreResult:
    """
    Score formula
    ─────────────
      score = max(Δprice%, 0) × price_w
            + log2(vol_ratio) × volume_w
            + momentum_norm   × momentum_w
            + rsi_pts         × rsi_w
    """
    price_change = pct_change(current_price, last_price)
    volume_ratio = (
        current_volume / last_volume
        if last_volume and last_volume > 0 else 1.0
    )

    # Momentum: rate-of-change over history window, normalised to [0, 1]
    momentum = 0.0
    if price_history and len(price_history) >= 2:
        roc = pct_change(price_history[-1], price_history[0])
        momentum = clamp(roc / 10.0, 0.0, 1.0)

    rsi = _compute_rsi(price_history or [], period=14)

    # RSI bonus/penalty
    rsi_pts = 0.0
    if not math.isnan(rsi):
        if 40 <= rsi <= 65:
            rsi_pts = 1.0       # trending sweet-spot
        elif 30 <= rsi < 40:
            rsi_pts = 0.5       # potential reversal zone
        elif rsi > 75:
            rsi_pts = -0.5      # overbought — discount score

    score = (
        max(price_change, 0.0) * weights.price_w
        + log2_safe(volume_ratio)  * weights.volume_w
        + momentum                 * weights.momentum_w
        + rsi_pts                  * weights.rsi_w
    )

    signal = _classify_signal(price_change, volume_ratio, momentum, rsi)

    return ScoreResult(
        score        = round(score, 4),
        price_change = price_change,
        volume_ratio = volume_ratio,
        momentum     = momentum,
        rsi          = rsi,
        signal       = signal,
        above_thresh = score >= weights.threshold,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Market-hours helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz=tz)


def is_market_open(cfg: MarketConfig) -> bool:
    """True only during the configured trading session (Mon–Fri)."""
    if FORCE_SCAN:
        return True
    now = _now(cfg.tz)
    if now.isoweekday() > 5:          # Sat=6, Sun=7
        return False
    open_t  = now.replace(hour=cfg.open_h,  minute=cfg.open_m,  second=0, microsecond=0)
    close_t = now.replace(hour=cfg.close_h, minute=cfg.close_m, second=0, microsecond=0)
    return open_t <= now <= close_t


# ══════════════════════════════════════════════════════════════════════════════
# State persistence
# ══════════════════════════════════════════════════════════════════════════════

def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _empty_market_state() -> dict[str, Any]:
    return {
        "memory":          {},     # ticker → [price, volume]
        "history_cache":   {},     # ticker → [float, ...]
        "baseline_done":   False,
        "alerted_today":   [],
        "alert_prices":    {},
        "eod_sent_today":  False,
    }


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "date":    _today_utc(),
        "markets": {
            "GSE": _empty_market_state(),
            "US":  _empty_market_state(),
        },
        "custom_watchlist": [],
    }


def load_state() -> dict[str, Any]:
    """
    Load bot_state.json.
    Falls back to a clean empty state when:
      • File missing (first ever run)
      • File corrupt / wrong version
      • Saved date ≠ today (automatic daily reset)
    """
    if not STATE_FILE.exists():
        logger.info("No state file — fresh start.")
        return _empty_state()

    try:
        raw: dict = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("State file unreadable (%s) — fresh start.", exc)
        return _empty_state()

    if raw.get("version") != STATE_VERSION:
        logger.info("State version mismatch — resetting.")
        return _empty_state()

    today = _today_utc()
    if raw.get("date") != today:
        logger.info("New day (%s → %s) — daily reset.", raw.get("date"), today)
        return _empty_state()

    # Ensure both market sub-dicts exist (safety for partial states)
    for key in ("GSE", "US"):
        raw["markets"].setdefault(key, _empty_market_state())

    logger.info(
        "State loaded — GSE baseline=%s alerted=%d | US baseline=%s alerted=%d",
        raw["markets"]["GSE"]["baseline_done"],
        len(raw["markets"]["GSE"]["alerted_today"]),
        raw["markets"]["US"]["baseline_done"],
        len(raw["markets"]["US"]["alerted_today"]),
    )
    return raw


def save_state(state: dict[str, Any]) -> None:
    """Atomic write: tmp file → rename (avoids corruption on crash)."""
    state["date"]    = _today_utc()
    state["version"] = STATE_VERSION
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
        logger.info("State saved → %s", STATE_FILE)
    except OSError as exc:
        logger.error("Could not save state: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StockSnapshot:
    ticker:        str
    price:         float
    volume:        float
    price_history: list[float]   # newest last; may be empty for GSE


def _fetch(
    url:     str,
    cfg:     MarketConfig,
    params:  dict | None = None,
) -> requests.Response | None:
    """GET with exponential back-off. Returns Response or None after exhausting retries."""
    headers = {**_session.headers, **cfg.extra_headers}
    backoff  = 2.0

    for attempt in range(1, cfg.api_retries + 1):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=cfg.api_timeout)
            r.raise_for_status()
            return r
        except requests.Timeout:
            logger.warning("Timeout %s (attempt %d/%d).", url, attempt, cfg.api_retries)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            logger.error("HTTP %s from %s (attempt %d/%d).", status, url, attempt, cfg.api_retries)
            if status and status < 500:
                return None
        except requests.RequestException as exc:
            logger.error("Request error (attempt %d/%d): %s", attempt, cfg.api_retries, exc)

        if attempt < cfg.api_retries:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    return None


def fetch_gse(cfg: MarketConfig) -> list[StockSnapshot]:
    r = _fetch(cfg.api_url, cfg)
    if r is None:
        return []

    try:
        raw: list[dict] = r.json()
    except ValueError:
        logger.error("GSE API returned non-JSON.")
        return []

    if not isinstance(raw, list):
        logger.error("GSE: expected list, got %s.", type(raw).__name__)
        return []

    out: list[StockSnapshot] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ticker = item.get("name")
        price  = to_float(item.get("price"))
        volume = to_float(item.get("volume"), default=0.0)

        if not ticker or price is None:
            continue
        if price < cfg.min_price or (volume or 0.0) < cfg.min_volume:
            continue

        out.append(StockSnapshot(ticker=ticker, price=price,
                                  volume=volume or 0.0, price_history=[]))

    logger.info("GSE: %d valid snapshots.", len(out))
    return out


def fetch_us(cfg: MarketConfig, tickers: list[str]) -> list[StockSnapshot]:
    """
    Batch-fetch from Yahoo Finance v8 spark endpoint.
    Splits into chunks of 80 to stay within URL-length limits.
    """
    if not tickers:
        return []

    all_snaps: list[StockSnapshot] = []

    for chunk in chunks(tickers, 80):
        r = _fetch(
            cfg.api_url,
            cfg,
            params={"symbols": ",".join(chunk), "range": "1mo", "interval": "1d"},
        )
        if r is None:
            continue

        try:
            body = r.json()
        except ValueError:
            logger.error("Yahoo Finance returned non-JSON.")
            continue

        # Yahoo v8 spark returns {"spark": {"result": {ticker: {...}, ...}}}
        # but occasionally wraps result in a list — handle both shapes
        raw_result = (body.get("spark") or {}).get("result") or {}

        if isinstance(raw_result, list):
            spark: dict[str, Any] = {
                item["symbol"]: item
                for item in raw_result
                if isinstance(item, dict) and "symbol" in item
            }
        else:
            spark = raw_result

        for ticker in chunk:
            entry = spark.get(ticker)
            if not entry:
                logger.debug("No Yahoo data for %s.", ticker)
                continue

            try:
                resp_list = entry.get("response") or []
                if not resp_list:
                    continue
                resp    = resp_list[0]
                meta    = resp.get("meta") or {}
                quote_0 = (resp.get("indicators") or {}).get("quote") or [{}]
                closes  = [c for c in (quote_0[0].get("close")  or []) if c is not None]
                volumes = [v for v in (quote_0[0].get("volume") or []) if v is not None]
            except (IndexError, AttributeError, TypeError) as exc:
                logger.debug("Malformed Yahoo entry for %s: %s", ticker, exc)
                continue

            if not closes:
                continue

            price  = to_float(meta.get("regularMarketPrice")) or closes[-1]
            volume = to_float(meta.get("regularMarketVolume")) or (volumes[-1] if volumes else 0.0)

            if price is None or float(price) < cfg.min_price:
                continue
            if (volume or 0.0) < cfg.min_volume:
                continue

            all_snaps.append(StockSnapshot(
                ticker        = ticker,
                price         = float(price),
                volume        = float(volume or 0.0),
                price_history = [float(c) for c in closes],
            ))

    logger.info("US: %d valid snapshots.", len(all_snaps))
    return all_snaps


# ══════════════════════════════════════════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════════════════════════════════════════

class TelegramClient:

    def __init__(self, token: str, chat_id: str) -> None:
        self.token   = token
        self.chat_id = chat_id
        self._ready  = False

    def validate(self) -> bool:
        """Confirm the token is live and CHAT_ID is not the bot's own ID."""
        if not self._token_ok():
            logger.error("BOT_TOKEN malformed (expected <digits>:<secret>).")
            return False

        url = f"https://api.telegram.org/bot{self.token}/getMe"
        try:
            r    = _session.get(url, timeout=10)
            body = r.json() if "application/json" in r.headers.get("content-type", "") else {}
        except requests.RequestException as exc:
            logger.error("getMe failed: %s", exc)
            return False

        if r.status_code != 200 or not body.get("ok"):
            logger.error("getMe returned HTTP %d — check BOT_TOKEN.", r.status_code)
            return False

        bot_id = str((body.get("result") or {}).get("id", ""))
        if bot_id and bot_id == self.chat_id.strip():
            logger.error(
                "CHAT_ID (%s) is the bot's own ID. "
                "Send /start to @userinfobot to get your personal ID.",
                bot_id,
            )
            return False

        logger.info("Telegram OK (bot id=%s).", bot_id)
        self._ready = True
        return True

    def send(self, text: str) -> bool:
        """Send HTML-formatted message. json= ensures correct Content-Type."""
        if not self._ready:
            logger.info("[TG suppressed] %s", text[:120])
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = _session.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            logger.error("sendMessage HTTP %d: %s", r.status_code, r.text[:300])
            return False
        except requests.RequestException as exc:
            logger.error("sendMessage exception: %s", exc)
            return False

    def handle_command(self, text: str, watchlist: list[str]) -> str | None:
        """Process /add, /remove, /list commands. Returns response message."""
        parts = text.strip().split()
        if not parts:
            return None

        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        if cmd == "/list":
            if not watchlist:
                return "📋 <b>Watchlist</b> is empty. Default US watchlist in use."
            return "📋 <b>Custom Watchlist</b>\n" + ", ".join(watchlist)

        if cmd == "/add":
            if not args:
                return "Usage: /add AAPL MSFT GOOGL"
            added = [t.upper() for t in args if t.isalpha()]
            for t in added:
                if t not in watchlist:
                    watchlist.append(t)
            return f"✅ Added: {', '.join(added)}"

        if cmd == "/remove":
            if not args:
                return "Usage: /remove AAPL"
            removed = []
            for t in args:
                t = t.upper()
                if t in watchlist:
                    watchlist.remove(t)
                    removed.append(t)
            if removed:
                return f"🗑️ Removed: {', '.join(removed)}"
            return "⚠️ Tickers not found in custom watchlist."

        if cmd == "/help":
            return (
                "📖 <b>Commands</b>\n"
                "/add TICKER... — Add stocks\n"
                "/remove TICKER... — Remove stocks\n"
                "/list — Show watchlist\n"
                "/help — Show this help"
            )

        return None

    def get_updates(self, offset: int = 0) -> list[dict]:
        """Poll for new command messages."""
        if not self._ready:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        try:
            r = _session.get(url, params={"timeout": 1, "offset": offset}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                return data.get("result", [])
        except requests.RequestException:
            pass
        return []

    def _token_ok(self) -> bool:
        """Telegram bot tokens are always '<numeric_bot_id>:<secret>' from @BotFather."""
        parts = (self.token or "").split(":", 1)
        return len(parts) == 2 and parts[0].isdigit() and len(parts[1]) >= 25


# ══════════════════════════════════════════════════════════════════════════════
# Alert message builders
# ══════════════════════════════════════════════════════════════════════════════

_SIGNAL_EMOJI = {
    SignalType.BREAKOUT:     "🚀",
    SignalType.ACCUMULATION: "🏦",
    SignalType.MOMENTUM:     "⚡",
    SignalType.REVERSAL:     "🔄",
    SignalType.NONE:         "📊",
}


def _alert_msg(
    cfg:    MarketConfig,
    ticker: str,
    result: ScoreResult,
    price:  float,
    volume: float,
) -> str:
    emoji  = _SIGNAL_EMOJI.get(result.signal, "📊")
    dir_e  = "📈" if result.price_change >= 0 else "📉"
    rsi_s  = f"{result.rsi:.1f}" if not math.isnan(result.rsi) else "N/A"
    mom_s  = f"{result.momentum * 100:.1f}%"

    return (
        f"{emoji} <b>{cfg.flag} {cfg.label} Signal</b>\n"
        f"<b>Ticker:</b> {ticker}\n"
        f"<b>Signal:</b> {result.signal.value}\n"
        "\n"
        f"<b>Price:</b> {cfg.currency} {price:.4f}  {dir_e} {result.price_change:+.2f}%\n"
        f"<b>Volume:</b> {int(volume):,}  ×{result.volume_ratio:.2f} avg\n"
        f"<b>Momentum:</b> {mom_s}   <b>RSI(14):</b> {rsi_s}\n"
        f"<b>Score:</b> {result.score:.2f}  (threshold {cfg.weights.threshold})\n"
        "\n"
        "⚡ Apply your own analysis before acting."
    )


def _eod_msg(cfg: MarketConfig, changes: dict[str, float]) -> str:
    flag = cfg.flag
    lines = [f"📋 <b>{flag} {cfg.label} — Daily Summary</b>\n"]
    if not changes:
        lines.append("No alerts were sent today.")
    else:
        for ticker, chg in sorted(changes.items(), key=lambda x: -x[1]):
            arrow = "📈" if chg >= 0 else "📉"
            lines.append(f"  {arrow} <b>{ticker}</b>: {chg:+.2f}% since alert")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Core scan logic
# ══════════════════════════════════════════════════════════════════════════════

def _get_snapshots(cfg: MarketConfig) -> list[StockSnapshot]:
    if cfg.id == MarketID.GSE:
        return fetch_gse(cfg)
    if cfg.id == MarketID.US:
        return fetch_us(cfg, US_WATCHLIST)
    return []


def run_scan(
    cfg:     MarketConfig,
    saved:   dict[str, Any],
    tg:      TelegramClient,
) -> None:
    """
    One complete scan cycle for a single market.
    Mutates `saved["markets"][key]` in-place so the caller can persist it.
    """
    key   = cfg.id.name           # "GSE" or "US"
    mdata = saved["markets"][key]

    snaps = _get_snapshots(cfg)
    if not snaps:
        logger.warning("[%s] No data returned — skipping scan.", key)
        return

    logger.info("[%s] Processing %d tickers.", key, len(snaps))

    memory:        dict[str, tuple[float, float]] = {
        k: (float(v[0]), float(v[1])) for k, v in mdata["memory"].items()
    }
    history_cache: dict[str, list[float]] = {
        k: [float(p) for p in v] for k, v in mdata["history_cache"].items()
    }
    alerted_today: set[str]  = set(mdata["alerted_today"])
    alert_prices:  dict[str, float] = dict(mdata["alert_prices"])
    baseline_done: bool       = mdata["baseline_done"]

    alerts_sent = 0

    for snap in snaps:
        last_price, last_vol = memory.get(snap.ticker, (0.0, 0.0))

        # Merge incoming history with cached history (keep latest 60 bars)
        if snap.price_history:
            merged = history_cache.get(snap.ticker, []) + snap.price_history
            history_cache[snap.ticker] = merged[-60:]
        combined = history_cache.get(snap.ticker)

        result = score_stock(
            current_price  = snap.price,
            current_volume = snap.volume,
            last_price     = last_price,
            last_volume    = last_vol,
            weights        = cfg.weights,
            price_history  = combined or None,
        )

        # Always update memory regardless of alert
        memory[snap.ticker] = (snap.price, snap.volume)

        should_alert = (
            baseline_done
            and result.above_thresh
            and result.signal != SignalType.NONE
            and snap.ticker not in alerted_today
        )

        if should_alert:
            msg  = _alert_msg(cfg, snap.ticker, result, snap.price, snap.volume)
            sent = tg.send(msg)
            if sent:
                alerted_today.add(snap.ticker)
                alert_prices[snap.ticker] = snap.price
                alerts_sent += 1
                logger.info(
                    "[%s] ✓ %s | %s | score=%.2f Δ%+.2f%% vol×%.1f RSI=%s",
                    key, snap.ticker, result.signal.value, result.score,
                    result.price_change, result.volume_ratio,
                    f"{result.rsi:.0f}" if not math.isnan(result.rsi) else "N/A",
                )

    if not baseline_done:
        logger.info("[%s] Baseline set (%d tickers). Alerts start next scan.", key, len(memory))

    elif alerts_sent == 0:
        logger.info("[%s] Scan complete — no signals above %.1f.", key, cfg.weights.threshold)

    # Write back into saved dict
    mdata["memory"]        = {k: list(v) for k, v in memory.items()}
    mdata["history_cache"] = dict(history_cache)
    mdata["baseline_done"] = True
    mdata["alerted_today"] = list(alerted_today)
    mdata["alert_prices"]  = dict(alert_prices)


def run_eod(
    cfg:   MarketConfig,
    saved: dict[str, Any],
    tg:    TelegramClient,
) -> None:
    """Send end-of-day P&L summary for alerted tickers (fires once per day)."""
    key   = cfg.id.name
    mdata = saved["markets"][key]

    if not mdata["baseline_done"] or mdata["eod_sent_today"]:
        return

    alerted_today = set(mdata["alerted_today"])
    if not alerted_today:
        mdata["eod_sent_today"] = True
        return

    # Fetch current prices for P&L calc
    snaps     = _get_snapshots(cfg)
    price_map = {s.ticker: s.price for s in snaps}
    changes: dict[str, float] = {}

    for ticker in alerted_today:
        alert_px   = mdata["alert_prices"].get(ticker)
        current_px = price_map.get(ticker)
        if alert_px and current_px and float(alert_px) > 0:
            changes[ticker] = pct_change(float(current_px), float(alert_px))

    tg.send(_eod_msg(cfg, changes))
    mdata["eod_sent_today"] = True
    logger.info("[%s] EOD summary sent (%d tickers).", key, len(changes))


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    logger.info(
        "=== Dual-Market Bot run start | UTC %s | FORCE_SCAN=%s ===",
        datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        FORCE_SCAN,
    )

    # ── Telegram ──────────────────────────────────────────────────────────
    tg = TelegramClient(token=BOT_TOKEN, chat_id=CHAT_ID)
    if tg.validate():
        tg.send(
            "🤖 <b>Dual-Market Bot Started</b>\n"
            f"Monitoring: 🇬🇭 GSE + 🇺🇸 US\n"
            f"Watchlist: {len(US_WATCHLIST)} US stocks\n"
            "Scanning markets...\n"
            "Send /help for commands."
        )

    # ── State ─────────────────────────────────────────────────────────────
    saved = load_state()

    # ── Process Telegram commands ───────────────────────────────────────
    custom_watchlist: list[str] = saved.get("custom_watchlist", [])
    last_update_id: int = saved.get("last_telegram_update_id", 0)
    if tg._ready:
        updates = tg.get_updates(last_update_id)
        for update in updates:
            last_update_id = update.get("update_id", 0) + 1
            msg = (update.get("message") or {}).get("text", "")
            chat = str((update.get("message") or {}).get("chat", {}).get("id", ""))
            if msg and chat == tg.chat_id:
                resp = tg.handle_command(msg, custom_watchlist)
                if resp:
                    tg.send(resp)
        saved["last_telegram_update_id"] = last_update_id
        saved["custom_watchlist"] = custom_watchlist

    # ── Determine open / closed markets ───────────────────────────────────
    open_markets   = [cfg for cfg in ALL_MARKETS if     is_market_open(cfg)]
    closed_markets = [cfg for cfg in ALL_MARKETS if not is_market_open(cfg)]

    logger.info(
        "Open: %s | Closed: %s",
        [c.id.name for c in open_markets]  or "none",
        [c.id.name for c in closed_markets] or "none",
    )

    if not open_markets:
        logger.info("Both markets closed — checking EOD summaries then exiting.")

    # ── Scan open markets ─────────────────────────────────────────────────
    for cfg in open_markets:
        run_scan(cfg, saved, tg)

    # ── EOD summary for closed markets that were active today ─────────────
    for cfg in closed_markets:
        run_eod(cfg, saved, tg)

    # ── Persist state ─────────────────────────────────────────────────────
    save_state(saved)

    logger.info("=== Run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())