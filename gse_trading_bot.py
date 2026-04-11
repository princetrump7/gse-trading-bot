import sys
import logging
import time
import json
import os
from datetime import datetime, time as dtime
from html import escape as html_escape
from typing import Any

import requests

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
# NOTE: Keeping these hardcoded per your preference. Consider moving to environment variables later.
BOT_TOKEN = "8742412969:AAF6IcaSQ2KMfX3ZYiIh9XPgbgWzN-NTWyQ"
# Your Telegram user id or group id — not the bot id (same number as in BOT_TOKEN prefix will 403).
CHAT_ID = "1751325678"

GSE_LIVE_URL = "https://dev.kwayisi.org/apis/gse/live"
SCORE_THRESHOLD = 10.0
POLL_INTERVAL_SECONDS = 600  # 10 minutes

market_memory = {}
first_scan_complete = False
alerted_today = set()

# Set True only after getMe succeeds at startup (invalid BOT_TOKEN → stays False).
TELEGRAM_ENABLED = False
# False when CHAT_ID equals the bot's own id (Telegram returns 403: bots can't message bots).
TELEGRAM_SEND_OK = True

DEBUG_LOG_PATH = os.path.join(os.path.dirname(__file__), "debug-5ad608.log")


def bot_token_looks_valid(token: str) -> bool:
    """Telegram bot tokens are always '<numeric_bot_id>:<secret>' from @BotFather."""
    if not isinstance(token, str):
        return False
    parts = token.split(":", 1)
    if len(parts) != 2:
        return False
    bot_id, secret = parts
    if not bot_id.isdigit() or not secret:
        return False
    return len(secret) >= 25


def debug_ndjson_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    run_id: str = "debug",
) -> None:
    """Append a single NDJSON debug event for the debug-mode workflow."""
    payload: dict[str, Any] = {
        "sessionId": "5ad608",
        "id": f"log_{time.time_ns()}",
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data or {},
        "runId": run_id,
        "hypothesisId": hypothesis_id,
    }
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Never let debug logging break the bot.
        pass


def send_telegram_msg(message: str) -> bool:
    """Send a message via Telegram bot API."""
    if not TELEGRAM_ENABLED or not TELEGRAM_SEND_OK:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    #region agent log
    debug_ndjson_log(
        hypothesis_id="TG_SEND_1",
        location="gse_trading_bot.py:send_telegram_msg:before_request",
        message="Sending Telegram sendMessage request",
        data={"parse_mode": payload.get("parse_mode"), "chat_id_len": len(str(CHAT_ID))},
    )
    #endregion
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            logger.error("Telegram API error %d: %s", r.status_code, r.text)
            #region agent log
            debug_ndjson_log(
                hypothesis_id="TG_SEND_1",
                location="gse_trading_bot.py:send_telegram_msg:after_response_error",
                message="Telegram sendMessage failed",
                data={"status_code": r.status_code, "response_prefix": r.text[:200]},
            )
            #endregion
            return False
        #region agent log
        debug_ndjson_log(
            hypothesis_id="TG_SEND_2",
            location="gse_trading_bot.py:send_telegram_msg:after_response_ok",
            message="Telegram sendMessage succeeded",
            data={"status_code": r.status_code},
        )
        #endregion
        return True
    except requests.RequestException as e:
        logger.error("Telegram send failed: %s", e)
        #region agent log
        debug_ndjson_log(
            hypothesis_id="TG_SEND_3",
            location="gse_trading_bot.py:send_telegram_msg:exception",
            message="Telegram request exception",
            data={"error": str(e)[:200]},
        )
        #endregion
        return False


def get_live_gse_data() -> list[dict]:
    """Fetch live stock data from GSE API."""
    # Retry once for transient failures (timeouts/5xx)
    for attempt in range(2):
        try:
            response = requests.get(GSE_LIVE_URL, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Validate response is a list
            if not isinstance(data, list):
                logger.error(
                    "Unexpected API response format: expected list, got %s",
                    type(data).__name__,
                )
                return []

            # Validate each item has required fields
            valid_items = []
            for item in data:
                if not isinstance(item, dict):
                    logger.warning("Skipping non-dict item in API response: %s", item)
                    continue
                if "name" not in item:
                    logger.warning("Skipping item without 'name' field: %s", item)
                    continue
                valid_items.append(item)

            return valid_items

        except requests.Timeout:
            logger.error("GSE API request timed out (attempt %d/2)", attempt + 1)
        except requests.HTTPError as e:
            # Retry once on server-side problems
            status = getattr(e.response, "status_code", None)
            logger.error("GSE API HTTP error (attempt %d/2): %s", attempt + 1, e)
            if status is not None and status < 500:
                return []
        except requests.RequestException as e:
            logger.error("GSE API request failed (attempt %d/2): %s", attempt + 1, e)
        except ValueError as e:
            logger.error("GSE API returned invalid JSON (attempt %d/2): %s", attempt + 1, e)
            return []

        # Backoff before retrying
        if attempt == 0:
            time.sleep(2)

    return []


def to_float(value, *, default: float | None = None) -> float | None:
    """Coerce an API value to float. Returns `default` if parsing fails."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace(",", "")
        return float(s)
    except (TypeError, ValueError):
        return default


def score_stock(current_price: float, current_volume: float, last_price: float, last_volume: float):
    """
    Calculate a stock score based on price change and volume ratio.

    Returns:
        tuple: (score, price_change_pct, volume_ratio)
    """
    # Guard against zero division
    if last_price and last_price > 0:
        price_change = ((current_price - last_price) / last_price) * 100
    else:
        price_change = 0.0

    if last_volume and last_volume > 0:
        volume_ratio = current_volume / last_volume
    else:
        volume_ratio = 1.0  # No previous data, assume baseline

    # Improved scoring: penalize negative price changes
    # Weight volume change and absolute price movement
    score = volume_ratio * 0.5 + price_change * 0.4

    return score, price_change, volume_ratio


def market_open() -> bool:
    """
    Check if the Ghana Stock Exchange market is currently open.
    Trading hours: Mon-Fri, 09:00 to 16:00 (local time).
    """
    now = datetime.now()

    # Weekday check (0=Monday, 4=Friday)
    if now.weekday() >= 5:
        return False

    start = dtime(9, 0, 0)
    end = dtime(16, 0, 0)
    return start <= now.time() <= end


def reset_daily_alerts():
    """Reset alerted stocks at the start of each new day."""
    global alerted_today
    if alerted_today:
        logger.info("Resetting daily alert tracker.")
        alerted_today.clear()


def check_market():
    """Scan GSE live data and trigger alerts for significant movements."""
    global first_scan_complete

    logger.info("Scanning GSE live data...")
    data = get_live_gse_data()

    if not data:
        logger.warning("No valid data received from GSE API.")
        return

    logger.info("Retrieved %d stocks from GSE API.", len(data))

    for item in data:
        ticker = item.get("name")
        current_price = to_float(item.get("price"), default=None)
        current_volume = to_float(item.get("volume"), default=None)

        if current_price is None or current_volume is None:
            logger.warning("Skipping %s due to unparseable price/volume.", ticker)
            continue

        last_price, last_volume = market_memory.get(ticker, (0.0, 0.0))

        score, price_change, volume_ratio = score_stock(
            current_price, current_volume, last_price, last_volume
        )

        # Only alert after we have baseline data (not on first scan)
        if first_scan_complete and score >= SCORE_THRESHOLD and ticker not in alerted_today:
            ticker_esc = html_escape(str(ticker))
            msg = (
                f"🚨 <b>GSE Signal: {ticker_esc}</b>\n"
                f"Price: {current_price:.2f}  Change: {price_change:.2f}%\n"
                f"Volume: {int(current_volume)}  x{volume_ratio:.1f}\n"
                f"Score: {score:.1f}\n"
                "Possible breakout/accumulation."
            )
            if send_telegram_msg(msg):
                alerted_today.add(ticker)
                logger.info("Alert sent for: %s (score=%.1f)", ticker, score)
            else:
                logger.warning("Failed to send alert for: %s", ticker)

        market_memory[ticker] = (current_price, current_volume)

    if not first_scan_complete:
        first_scan_complete = True
        logger.info(
            "Baseline scan complete. Loaded %d stocks. Alerts will start on next scan.",
            len(market_memory),
        )


def telegram_get_me(*, run_id: str = "debug") -> tuple[int | None, dict[str, Any] | None]:
    """Probe token validity using getMe. Helpful when sendMessage returns 404."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        #region agent log
        debug_ndjson_log(
            hypothesis_id="TG_GETME_1",
            location="gse_trading_bot.py:telegram_get_me:before_request",
            message="Calling Telegram getMe probe",
            data={"method": "getMe"},
            run_id=run_id,
        )
        #endregion
        r = requests.get(url, timeout=10)
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = {"raw_prefix": r.text[:200]}

        #region agent log
        debug_ndjson_log(
            hypothesis_id="TG_GETME_1",
            location="gse_trading_bot.py:telegram_get_me:after_response",
            message="Telegram getMe probe finished",
            data={"status_code": status, "body_prefix": json.dumps(body)[:200]},
            run_id=run_id,
        )
        #endregion
        return status, body if isinstance(body, dict) else None
    except requests.RequestException as e:
        #region agent log
        debug_ndjson_log(
            hypothesis_id="TG_GETME_2",
            location="gse_trading_bot.py:telegram_get_me:exception",
            message="Telegram getMe probe exception",
            data={"error": str(e)[:200]},
            run_id=run_id,
        )
        #endregion
        return None, None


if __name__ == "__main__":
    logger.info("GSE Trading Bot started. Polling every %d seconds.", POLL_INTERVAL_SECONDS)
    try:
        if not bot_token_looks_valid(BOT_TOKEN):
            logger.error(
                "BOT_TOKEN must be the full string from @BotFather: <digits>:<secret> "
                "(for example 123456789:AAH...). If yours has no ':', you copied only part of the token."
            )
            #region agent log
            debug_ndjson_log(
                hypothesis_id="TG_TOKEN_FORMAT",
                location="gse_trading_bot.py:__main__:token_format",
                message="BOT_TOKEN format invalid (missing id:secret shape)",
                data={"has_colon": ":" in str(BOT_TOKEN)},
            )
            #endregion
            TELEGRAM_ENABLED = False
        else:
            #region agent log
            debug_ndjson_log(
                hypothesis_id="TG_TOKEN_FORMAT",
                location="gse_trading_bot.py:__main__:token_format",
                message="BOT_TOKEN format looks valid; probing getMe",
                data={"looks_valid": True},
            )
            #endregion
            # Runtime evidence: getMe 404 means Telegram rejects the token (not chat_id / parse_mode).
            status, body = telegram_get_me(run_id="debug_startup")
            TELEGRAM_ENABLED = (
                status == 200
                and isinstance(body, dict)
                and body.get("ok") is True
            )
            if not TELEGRAM_ENABLED:
                logger.error(
                    "Telegram rejected BOT_TOKEN (getMe HTTP %s). "
                    "Open @BotFather, create a bot or copy the token again, then update BOT_TOKEN.",
                    status if status is not None else "error",
                )
            else:
                logger.info("Telegram BOT_TOKEN validated (getMe ok).")
                bot_user_id = None
                if isinstance(body, dict) and isinstance(body.get("result"), dict):
                    bot_user_id = body["result"].get("id")
                if bot_user_id is not None and str(CHAT_ID).strip() == str(bot_user_id):
                    TELEGRAM_SEND_OK = False
                    logger.error(
                        "CHAT_ID (%s) is the bot's own Telegram user id. You cannot message a bot from a bot. "
                        "Set CHAT_ID to YOUR personal user id (message @userinfobot and use the number it shows), "
                        "or a group/channel id where the bot is allowed to post.",
                        bot_user_id,
                    )
                    #region agent log
                    debug_ndjson_log(
                        hypothesis_id="TG_CHAT_BOT_SELF",
                        location="gse_trading_bot.py:__main__:chat_vs_bot",
                        message="CHAT_ID equals bot id; sendMessage would return 403",
                        data={"bot_user_id": bot_user_id},
                    )
                    #endregion
                else:
                    TELEGRAM_SEND_OK = True
                    send_telegram_msg("🤖 GSE Trading Bot started and monitoring markets.")
    except Exception:
        logger.warning("Could not send startup notification to Telegram.")

    last_reset_day = datetime.now().day

    while True:
        try:
            now = datetime.now()

            # Reset alerts at the start of each new day
            if now.day != last_reset_day:
                reset_daily_alerts()
                last_reset_day = now.day

            if market_open():
                check_market()
            else:
                logger.info("Market closed. Waiting for next poll.")

            time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            send_telegram_msg("🤖 GSE Trading Bot stopped.")
            sys.exit(0)
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)

