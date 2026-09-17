# 1-Hour Range Strategy Bot (BTCUSD)

Telegram alert bot implementing the "1-Hour Range Strategy" from your PDF.

## Strategy recap
- **Range**: High/Low of the first fully-closed 1H candle of the day, NY timezone (00:00–01:00 NY)
- **Trigger TF**: 5-minute
- **Breakout**: 5m candle *closes* fully outside the range (wick doesn't count)
- **Re-entry**: a later 5m candle *closes* back inside the range, same day
  - Close above range → later close back inside = **SHORT**
  - Close below range → later close back inside = **LONG**
- **SL**: extreme price reached during the breakout leg
- **TP**: 2R (2x the stop distance)
- Multiple setups per day are allowed; state resets each new NY day.

This bot **only sends Telegram alerts** — it does not place trades on any account.

## Setup

1. Get a Telegram bot token from [@BotFather](https://t.me/BotFather) (or reuse one you already have).
2. Get your chat ID (message [@userinfobot](https://t.me/userinfobot) or use your existing chat ID `6329363443` if you want alerts in the same place as richyways_bot).
3. Set environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

## Run locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python bot.py
```

## Deploy on Railway (same pattern as your other bots)

1. Push this folder to a GitHub repo.
2. Create a new Railway project → Deploy from GitHub repo.
3. Add environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Railway's Variables tab.
4. Railway will use the `Procfile` to run it as a worker (no web port needed).

## Notes / things to watch

- Data source is Yahoo Finance (`BTC-USD`) via `yfinance`, same as your other range-tracking bots — free but can have brief gaps/delays. For tighter accuracy you could swap in a Binance/Bybit public API feed later.
- The PDF's backtest win rates (reported in the 40–60% range in the source video) were from a small sample — treat these as alerts to manually confirm, not blind auto-entries.
- Because this trades off *closed* candles only, signals fire a few seconds after each 5m close — that's intentional per the strategy rules (wick breaks don't count).
