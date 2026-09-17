"""
1-Hour Range Strategy Bot (BTCUSD) - Telegram Alerts Only
===========================================================

Strategy (from Strategy_Components.pdf):
- Setup TF: 1H, timezone = New York
- Range = High/Low of the FIRST fully-closed 1H candle of the NY day (00:00-01:00 NY)
- Execution TF: 5m
- Breakout: 5m candle CLOSES fully outside the 1H range (wick alone doesn't count)
- Re-entry: a LATER 5m candle CLOSES back inside the range, same trading day
  -> Short trigger: closed above range high, then closed back inside  => SHORT
  -> Long trigger:  closed below range low, then closed back inside   => LONG
- SL: extreme of the breakout move (highest high / lowest low reached during breakout)
- TP: 2R (2x the SL distance)
- Multiple valid setups per day are allowed.

This bot only sends Telegram alerts - it does not place trades.
"""

import os
import time
import logging
from datetime import datetime, timedelta
import pytz
import yfinance as yf
import pandas as pd
import requests

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "BTC-USD"          # yfinance ticker for BTCUSD
DISPLAY_SYMBOL = "BTCUSD"
NY_TZ = pytz.timezone("America/New_York")
POLL_SECONDS = 30           # how often to check for new closed 5m candles
RR = 2.0                    # take-profit risk:reward multiple

STATE = {
    "date": None,           # NY calendar date the current range belongs to
    "range_high": None,
    "range_low": None,
    "range_ready": False,   # True once first 1H candle of the day has closed
    "breakout_dir": None,   # "up" or "down" once a breakout close happens
    "breakout_extreme": None,  # extreme price reached during the breakout leg
    "last_5m_ts": None,     # last processed 5m candle timestamp
    "last_1h_ts": None,     # last processed 1h candle timestamp
    "trades_today": 0,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hourrange_bot")


def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code != 200:
            log.warning(f"Telegram send failed: {r.text}")
    except Exception as e:
        log.warning(f"Telegram send error: {e}")


def now_ny():
    return datetime.now(NY_TZ)


def _flatten_columns(df):
    """Newer yfinance versions return MultiIndex columns even for a single ticker."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def get_1h_data():
    """Fetch recent 1H candles, indexed in NY time."""
    df = yf.download(SYMBOL, period="5d", interval="1h", progress=False, auto_adjust=False)
    if df.empty:
        return df
    df = _flatten_columns(df)
    df.index = df.index.tz_convert(NY_TZ)
    return df


def get_5m_data():
    """Fetch recent 5m candles, indexed in NY time."""
    df = yf.download(SYMBOL, period="2d", interval="5m", progress=False, auto_adjust=False)
    if df.empty:
        return df
    df = _flatten_columns(df)
    df.index = df.index.tz_convert(NY_TZ)
    return df


def reset_state_for_new_day(day):
    STATE["date"] = day
    STATE["range_high"] = None
    STATE["range_low"] = None
    STATE["range_ready"] = False
    STATE["breakout_dir"] = None
    STATE["breakout_extreme"] = None
    STATE["trades_today"] = 0
    log.info(f"New NY trading day detected: {day}. State reset.")


def update_range(df_1h):
    """Find the first FULLY CLOSED 1H candle (00:00-01:00 NY) of today and lock the range."""
    today = now_ny().date()

    if STATE["date"] != today:
        reset_state_for_new_day(today)

    if STATE["range_ready"]:
        return

    # Look for the candle whose NY-local hour == 0 (00:00) for today, and which has fully closed
    for ts, row in df_1h.iterrows():
        if ts.date() == today and ts.hour == 0:
            candle_close_time = ts + timedelta(hours=1)
            if now_ny() >= candle_close_time:
                STATE["range_high"] = float(row["High"])
                STATE["range_low"] = float(row["Low"])
                STATE["range_ready"] = True
                STATE["last_1h_ts"] = ts
                msg = (
                    f"📊 *{DISPLAY_SYMBOL} 1H Range Locked* ({today})\n"
                    f"High: `{STATE['range_high']:.2f}`\n"
                    f"Low: `{STATE['range_low']:.2f}`\n"
                    f"Watching 5m for breakout + re-entry..."
                )
                log.info(msg.replace("\n", " | "))
                send_telegram(msg)
            break


def check_breakout_and_entry(df_5m):
    """Process newly closed 5m candles for breakout / re-entry logic."""
    if not STATE["range_ready"]:
        return

    today = now_ny().date()
    rh, rl = STATE["range_high"], STATE["range_low"]

    for ts, row in df_5m.iterrows():
        if ts.date() != today:
            continue
        if STATE["last_5m_ts"] is not None and ts <= STATE["last_5m_ts"]:
            continue

        candle_close_time = ts + timedelta(minutes=5)
        if now_ny() < candle_close_time:
            continue  # not fully closed yet

        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        # --- No breakout yet: look for a full-body close outside the range ---
        if STATE["breakout_dir"] is None:
            if close > rh:
                STATE["breakout_dir"] = "up"
                STATE["breakout_extreme"] = high
                send_telegram(
                    f"⚠️ *{DISPLAY_SYMBOL} Breakout ABOVE range*\n"
                    f"5m close: `{close:.2f}` > High `{rh:.2f}`\n"
                    f"Watching for re-entry (close back inside) → SHORT setup"
                )
            elif close < rl:
                STATE["breakout_dir"] = "down"
                STATE["breakout_extreme"] = low
                send_telegram(
                    f"⚠️ *{DISPLAY_SYMBOL} Breakout BELOW range*\n"
                    f"5m close: `{close:.2f}` < Low `{rl:.2f}`\n"
                    f"Watching for re-entry (close back inside) → LONG setup"
                )
        else:
            # Already broken out - update extreme, watch for re-entry or invalidation
            if STATE["breakout_dir"] == "up":
                STATE["breakout_extreme"] = max(STATE["breakout_extreme"], high)
                if rl < close < rh:
                    # Re-entry confirmed -> SHORT signal
                    sl = STATE["breakout_extreme"]
                    entry = close
                    risk = sl - entry
                    tp = entry - RR * risk
                    STATE["trades_today"] += 1
                    send_telegram(
                        f"🔴 *SELL (SHORT) - {DISPLAY_SYMBOL}*\n"
                        f"Entry (5m close back inside): `{entry:.2f}`\n"
                        f"Stop-Loss (breakout extreme): `{sl:.2f}`\n"
                        f"Take-Profit (2R): `{tp:.2f}`\n"
                        f"Time: {ts.strftime('%Y-%m-%d %H:%M')} NY\n"
                        f"Setup #{STATE['trades_today']} today"
                    )
                    STATE["breakout_dir"] = None
                    STATE["breakout_extreme"] = None
            elif STATE["breakout_dir"] == "down":
                STATE["breakout_extreme"] = min(STATE["breakout_extreme"], low)
                if rl < close < rh:
                    # Re-entry confirmed -> LONG signal
                    sl = STATE["breakout_extreme"]
                    entry = close
                    risk = entry - sl
                    tp = entry + RR * risk
                    STATE["trades_today"] += 1
                    send_telegram(
                        f"🟢 *BUY (LONG) - {DISPLAY_SYMBOL}*\n"
                        f"Entry (5m close back inside): `{entry:.2f}`\n"
                        f"Stop-Loss (breakout extreme): `{sl:.2f}`\n"
                        f"Take-Profit (2R): `{tp:.2f}`\n"
                        f"Time: {ts.strftime('%Y-%m-%d %H:%M')} NY\n"
                        f"Setup #{STATE['trades_today']} today"
                    )
                    STATE["breakout_dir"] = None
                    STATE["breakout_extreme"] = None

        STATE["last_5m_ts"] = ts


def main():
    log.info("1-Hour Range Strategy bot starting (BTCUSD, Telegram alerts only)...")
    send_telegram(f"🤖 1-Hour Range Bot started for {DISPLAY_SYMBOL}. Monitoring NY midnight 1H range + 5m breakout/re-entry.")

    while True:
        try:
            df_1h = get_1h_data()
            if not df_1h.empty:
                update_range(df_1h)

            df_5m = get_5m_data()
            if not df_5m.empty:
                check_breakout_and_entry(df_5m)

        except Exception as e:
            log.error(f"Error in main loop: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
