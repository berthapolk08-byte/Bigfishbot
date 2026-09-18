"""
4-Hour Range Strategy Bot (BTCUSD) - Telegram Alerts Only
===========================================================

Strategy (from Strategy_Components.pdf):
- Setup TF: 4H, timezone = New York
- Range = High/Low of the FIRST fully-closed 4H candle of the NY day (00:00-04:00 NY)
- Execution TF: 5m
- Breakout: 5m candle CLOSES fully outside the 4H range (wick alone doesn't count)
- Re-entry: a LATER 5m candle CLOSES back inside the range, same trading day
  -> Short trigger: closed above range high, then closed back inside  => SHORT
  -> Long trigger:  closed below range low, then closed back inside   => LONG
- SL: extreme of the breakout move (highest high / lowest low reached during breakout)
- TP: 2R (2x the SL distance)
- Multiple valid setups per day are allowed.

This bot only sends Telegram alerts - it does not place trades.

Data source: Binance public REST API (spot BTCUSDT klines) - real-time, reliable,
and far more accurate/liquid than delayed feeds like Yahoo Finance for crypto.
"""

import os
import time
import logging
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BINANCE_SYMBOL = "BTCUSDT"   # Binance spot ticker
DISPLAY_SYMBOL = "BTCUSD"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
NY_TZ = pytz.timezone("America/New_York")
RANGE_HOURS = 4              # first N hours of the NY day define the range
POLL_SECONDS = 20           # how often to check for new closed 5m candles
RR = 2.0                    # take-profit risk:reward multiple

STATE = {
    "date": None,           # NY calendar date the current range belongs to
    "range_high": None,
    "range_low": None,
    "range_ready": False,   # True once first 4H block of the day has fully closed
    "breakout_dir": None,   # "up" or "down" once a breakout close happens
    "breakout_extreme": None,  # extreme price reached during the breakout leg
    "last_5m_ts": None,     # last processed 5m candle timestamp
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


def fetch_klines(interval: str, limit: int) -> pd.DataFrame:
    """Fetch candles from Binance public API, indexed by candle OPEN time in NY timezone."""
    params = {"symbol": BINANCE_SYMBOL, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        return pd.DataFrame()

    rows = []
    for k in data:
        open_time = datetime.fromtimestamp(k[0] / 1000, tz=pytz.UTC).astimezone(NY_TZ)
        rows.append({
            "open_time": open_time,
            "Open": float(k[1]),
            "High": float(k[2]),
            "Low": float(k[3]),
            "Close": float(k[4]),
        })
    df = pd.DataFrame(rows).set_index("open_time")
    return df


def get_1h_data():
    """Fetch recent 1H candles from Binance, indexed in NY time."""
    return fetch_klines("1h", limit=72)  # last 3 days


def get_5m_data():
    """Fetch recent 5m candles from Binance, indexed in NY time."""
    return fetch_klines("5m", limit=576)  # last 2 days


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
    """Find the first FULLY CLOSED 4H block (00:00-04:00 NY) of today and lock the range,
    by combining the four 1h candles (hours 0,1,2,3) that make up that block."""
    today = now_ny().date()

    if STATE["date"] != today:
        reset_state_for_new_day(today)

    if STATE["range_ready"]:
        return

    block_end = NY_TZ.localize(datetime.combine(today, datetime.min.time())) + timedelta(hours=RANGE_HOURS)
    if now_ny() < block_end:
        return  # the 4H block hasn't fully closed yet

    todays_morning = df_1h[(df_1h.index.date == today) & (df_1h.index.hour < RANGE_HOURS)]
    if len(todays_morning) < RANGE_HOURS:
        log.warning(f"Only {len(todays_morning)}/{RANGE_HOURS} hourly candles available for today's 4H range yet.")
        return

    STATE["range_high"] = float(todays_morning["High"].max())
    STATE["range_low"] = float(todays_morning["Low"].min())
    STATE["range_ready"] = True
    msg = (
        f"📊 *{DISPLAY_SYMBOL} 4H Range Locked* ({today})\n"
        f"High: `{STATE['range_high']:.2f}`\n"
        f"Low: `{STATE['range_low']:.2f}`\n"
        f"Watching 5m for breakout + re-entry..."
    )
    log.info(msg.replace("\n", " | "))
    send_telegram(msg)


def check_breakout_and_entry(df_5m, silent=False):
    """Process newly closed 5m candles for breakout / re-entry logic.
    When silent=True, state is updated (breakout tracking, trade count) but no
    Telegram messages are sent - used to catch up on history after a restart
    without replaying old signals as if they just happened."""
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
                if not silent:
                    send_telegram(
                        f"⚠️ *{DISPLAY_SYMBOL} Breakout ABOVE range*\n"
                        f"5m close: `{close:.2f}` > High `{rh:.2f}`\n"
                        f"Watching for re-entry (close back inside) → SHORT setup"
                    )
            elif close < rl:
                STATE["breakout_dir"] = "down"
                STATE["breakout_extreme"] = low
                if not silent:
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
                    if not silent:
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
                    if not silent:
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
    log.info("4-Hour Range Strategy bot starting (BTCUSD, Telegram alerts only)...")

    # --- Silent catch-up: figure out today's range and any in-progress breakout
    # state WITHOUT replaying historical signals as if they just happened. ---
    try:
        df_1h = get_1h_data()
        if not df_1h.empty:
            update_range(df_1h)  # this DOES send one "range locked" message, which is fine

        df_5m = get_5m_data()
        if not df_5m.empty:
            check_breakout_and_entry(df_5m, silent=True)
        log.info(
            f"Catch-up complete. breakout_dir={STATE['breakout_dir']}, "
            f"trades_today={STATE['trades_today']}, last_5m_ts={STATE['last_5m_ts']}"
        )
    except Exception as e:
        log.error(f"Error during startup catch-up: {e}")

    send_telegram(f"🤖 4-Hour Range Bot (re)started for {DISPLAY_SYMBOL}. Live signals from now on.")

    while True:
        try:
            df_1h = get_1h_data()
            if not df_1h.empty:
                update_range(df_1h)

            df_5m = get_5m_data()
            if not df_5m.empty:
                check_breakout_and_entry(df_5m, silent=False)

        except Exception as e:
            log.error(f"Error in main loop: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
