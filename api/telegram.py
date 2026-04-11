"""
Vercel serverless function for Telegram bot commands.
Handles incoming webhook updates from Telegram.
"""

from gse_trading_bot import (
    TelegramClient,
    load_state,
    save_state,
    BOT_TOKEN,
    CHAT_ID,
    US_WATCHLIST,
)

def main(request):
    if request.method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    tg = TelegramClient(token=BOT_TOKEN, chat_id=CHAT_ID)
    if not tg.validate():
        return {"statusCode": 500, "body": "Bot validation failed"}

    try:
        body = request.get_json()
    except Exception:
        return {"statusCode": 400, "body": "Invalid JSON"}

    message = (body.get("message") or {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))

    if text and chat_id == tg.chat_id:
        saved = load_state()
        custom_watchlist = saved.get("custom_watchlist", [])
        combined_watchlist = custom_watchlist if custom_watchlist else US_WATCHLIST
        resp = tg.handle_command(text, combined_watchlist)

        if resp:
            tg.send(resp)
            if custom_watchlist:
                saved["custom_watchlist"] = custom_watchlist
                save_state(saved)

    return {"statusCode": 200, "body": "OK"}