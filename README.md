# GSE Swing Trading Signal Bot

This bot scans a GSE watchlist for swing trading signals based on a combination of technical indicators. It runs as a scheduled GitHub Action, ensuring reliable and automated execution during market hours.

## How It Works

The bot fetches live market data, analyzes it using RSI, EMA, close-to-close volatility, and a lightweight Hidden Markov Model (HMM) regime filter, then sends potential `BUY` or `SELL` signals to a specified Telegram channel.

- **Strategy**: Swing trading
- **Indicators**:
  - RSI-14
  - EMA-9 / EMA-21 crossover
  - 14-period close-to-close volatility for Stop Loss/Take Profit sizing
  - Volume confirmation
  - Two-state HMM regime confirmation
- **Execution**: Runs every 10 minutes via GitHub Actions on weekdays.
- **Scope**: Only scans the configured watchlist.

## Setup

1.  **Create a Telegram Bot:**
    -   Talk to the [BotFather](https://t.me/botfather) on Telegram.
    -   Create a new bot to get your `BOT_TOKEN`.
    -   Create a public or private channel for the bot to post messages in.
    -   Add your bot to the channel as an administrator.
    -   Find your `CHAT_ID`. For a public channel, it's the channel username (e.g., `@my_channel_name`). For a private channel, you can find it by other means (e.g. by using another bot to forward a message from the channel).

2.  **Add GitHub Secrets:**
    -   In your GitHub repository, go to `Settings` > `Secrets and variables` > `Actions`.
    -   Create these repository secrets:
        -   `BOT_TOKEN`: Your Telegram bot token.
        -   `CHAT_ID`: The ID of the Telegram channel where signals will be sent.
    -   Optional repository variables:
        -   `MIN_VOLUME`: Minimum live volume required before a signal can trigger.
        -   `ALERT_COOLDOWN_MINUTES`: Cooldown window for repeated same-direction alerts on a ticker.
        -   `MAX_HISTORY`: Maximum number of stored price/volume points per ticker.
        -   `HMM_MIN_HISTORY`: Minimum history length required before HMM confirmation is active.
        -   `HMM_CONFIDENCE_THRESHOLD`: Minimum regime probability required for bullish or bearish HMM confirmation.

3.  **Enable GitHub Actions:**
    -   Ensure that GitHub Actions are enabled for your repository. The workflow in `.github/workflows/gse-bot.yml` will automatically start running on its schedule.

## How State is Managed

GitHub Actions runners are stateless, meaning they start from a clean slate on every run. To maintain the price history needed for calculating indicators like EMA and RSI, the bot uses GitHub Actions artifacts:

1.  **After each run**, the script saves the updated price and volume history to `history.json` and `volume.json`, and the last sent alerts to `alert_state.json`.
2.  These files are **uploaded as artifacts**.
3.  **At the beginning of the next run**, the workflow downloads these artifacts, allowing the bot to load the history and continue its calculations.

This approach provides a simple, file-based persistence mechanism that is well-suited for the GitHub Actions environment.

## Disclaimer

This bot is for informational and educational purposes only. It is not financial advice. Trading financial markets involves significant risk. Always do your own research and manage your risk appropriately.
