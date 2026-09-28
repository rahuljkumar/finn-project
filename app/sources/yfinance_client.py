"""EOD price/volume fetch via yfinance, plus rolling-average and
threshold calculations used by the alerts pipeline."""

import logging

import pandas as pd
import yfinance as yf

from app.config import ALERT_PRICE_MOVE_PCT, ALERT_VOLUME_MULTIPLE
from app.db import get_conn

logger = logging.getLogger(__name__)

ROLLING_WINDOW = 20


def _yf_symbol(ticker: str, exchange: str = "NSE") -> str:
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    return f"{ticker}{suffix}"


def fetch_history(ticker: str, exchange: str = "NSE", period: str = "3mo") -> pd.DataFrame:
    """Fetch EOD OHLCV history and compute rolling volume average + daily % change."""
    symbol = _yf_symbol(ticker, exchange)
    df = yf.Ticker(symbol).history(period=period, interval="1d")
    if df.empty:
        logger.warning("No yfinance data for %s", symbol)
        return df

    df = df.reset_index()
    df["avg_volume_20d"] = df["Volume"].rolling(ROLLING_WINDOW, min_periods=5).mean()
    df["pct_change"] = df["Close"].pct_change() * 100
    return df


def persist_history(ticker: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    with get_conn() as conn:
        for _, row in df.iterrows():
            conn.execute(
                """
                INSERT INTO prices (ticker, date, close, volume, avg_volume_20d, pct_change)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET
                    close=excluded.close,
                    volume=excluded.volume,
                    avg_volume_20d=excluded.avg_volume_20d,
                    pct_change=excluded.pct_change
                """,
                (
                    ticker,
                    row["Date"].strftime("%Y-%m-%d"),
                    float(row["Close"]) if pd.notna(row["Close"]) else None,
                    int(row["Volume"]) if pd.notna(row["Volume"]) else None,
                    float(row["avg_volume_20d"]) if pd.notna(row["avg_volume_20d"]) else None,
                    float(row["pct_change"]) if pd.notna(row["pct_change"]) else None,
                ),
            )


def refresh_ticker(ticker: str, exchange: str = "NSE") -> pd.DataFrame:
    df = fetch_history(ticker, exchange)
    persist_history(ticker, df)
    return df


def detect_anomaly(
    ticker: str,
    volume_multiple: float | None = None,
    price_move_pct: float | None = None,
) -> dict | None:
    """Check the latest EOD row against the volume/price thresholds.
    Defaults come from config, but callers (the /alerts route) can pass
    user-chosen overrides -- the brief requires these be configurable,
    not just fixed at deploy time."""
    volume_multiple = ALERT_VOLUME_MULTIPLE if volume_multiple is None else volume_multiple
    price_move_pct = ALERT_PRICE_MOVE_PCT if price_move_pct is None else price_move_pct

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    if row is None or row["avg_volume_20d"] is None:
        return None

    reasons = []
    if row["avg_volume_20d"] and row["volume"] > volume_multiple * row["avg_volume_20d"]:
        reasons.append(
            f"Volume {row['volume']:,} is {row['volume'] / row['avg_volume_20d']:.1f}x its 20-day average"
        )
    if row["pct_change"] is not None and abs(row["pct_change"]) >= price_move_pct:
        reasons.append(f"Price moved {row['pct_change']:+.1f}% in a day")

    if not reasons:
        return None

    return {
        "ticker": ticker,
        "date": row["date"],
        "close": row["close"],
        "volume": row["volume"],
        "pct_change": row["pct_change"],
        "reasons": reasons,
    }
