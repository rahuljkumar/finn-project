"""Free official NSE end-of-day Full Bhavcopy reports for all stocks.

URLs and columns verified against NSE's All Reports page. Prices are EOD,
not streaming quotes. Cache only valid reports; unavailable dates are retried
later because today's final report may not have been published yet.
"""

import csv
import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from app.config import DATA_DIR
from app.db import get_conn
from app.storage import atomic_write

logger = logging.getLogger(__name__)
REPORT_DIR = DATA_DIR / "nse_reports"
IST = timezone(timedelta(hours=5, minutes=30))


def parse_report(content: bytes, day, tickers: set[str]) -> dict[str, dict]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), skipinitialspace=True)
    required = {"SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "CLOSE_PRICE", "TTL_TRD_QNTY"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Unexpected NSE report columns")
    result = {}
    for row in reader:
        ticker = row["SYMBOL"].strip()
        if ticker not in tickers or row["SERIES"].strip() != "EQ":
            continue
        if datetime.strptime(row["DATE1"].strip(), "%d-%b-%Y").date() != day:
            raise ValueError("NSE report date does not match requested day")
        close, previous, volume = float(row["CLOSE_PRICE"]), float(row["PREV_CLOSE"]), int(row["TTL_TRD_QNTY"])
        if not math.isfinite(close) or not math.isfinite(previous) or close <= 0 or previous < 0 or volume < 0:
            raise ValueError("Invalid NSE price or volume")
        result[ticker] = {
            "Date": pd.Timestamp(day), "Close": close, "Volume": volume,
            "pct_change": (close / previous - 1) * 100 if previous > 0 else None,
        }
    return result


def _report(day, tickers: set[str], revalidate: bool = False) -> dict[str, dict]:
    path = REPORT_DIR / f"{day.isoformat()}.csv"
    if path.exists() and not revalidate:
        return parse_report(path.read_bytes(), day, tickers)
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv"
    response = requests.get(url, timeout=(5, 10), headers={"User-Agent": "Mozilla/5.0 FINN/1.0"})
    response.raise_for_status()
    rows = parse_report(response.content, day, tickers)
    if rows:
        atomic_write(path, response.content)
    return rows


def fetch_portfolio_history(tickers: list[str], today=None) -> dict[str, pd.DataFrame]:
    today = today or datetime.now(IST).date()
    # Bootstrap 20 trading sessions once. With an existing history, only
    # catch up from the oldest stock's latest date (including missed days).
    with get_conn() as conn:
        counts = {r["ticker"]: (r["n"], r["latest"]) for r in conn.execute(
            "SELECT ticker, COUNT(*) AS n, MAX(date) AS latest FROM prices GROUP BY ticker"
        )}
        existing = {t: [dict(r) for r in conn.execute(
            "SELECT * FROM prices WHERE ticker = ? ORDER BY date DESC LIMIT 25", (t,)
        )] for t in tickers}
    bootstrap = any(counts.get(t, (0, None))[0] < 20 for t in tickers)
    oldest = min((counts[t][1] for t in tickers if t in counts), default=today.isoformat())
    lookback = 45 if bootstrap else min(45, max(7, (today - datetime.fromisoformat(oldest).date()).days + 1))
    # Include weekends: NSE occasionally holds special trading sessions.
    days = [today - timedelta(days=i) for i in range(lookback)]
    collected = {t: {} for t in tickers}

    def fetch(day):
        try:
            return day, _report(day, set(tickers), revalidate=day in probe)
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            logger.debug("NSE EOD report unavailable for %s: %s", day, exc)
            return day, {}

    # Revalidate the latest few dates against the actual source. Previously
    # cached reports must not mask a current outage as a successful refresh.
    # If blocked, stop rather than issuing the entire backfill.
    probe = days[:5]
    with ThreadPoolExecutor(max_workers=3) as pool:
        reports = list(pool.map(fetch, probe))
        if any(rows for _, rows in reports):
            reports.extend(pool.map(fetch, days[5:]))
    successful_dates = {day.isoformat() for day, rows in reports if rows}
    if not successful_dates:
        return {}
    latest_download = max(successful_dates)
    for day, rows in reports:
        for t, row in rows.items():
            collected[t][day.isoformat()] = row

    histories = {}
    for ticker, rows in collected.items():
        # A missing stock in the newest available report is a partial failure,
        # not permission to overwrite it with an older provider snapshot.
        if latest_download not in rows:
            continue
        for old in existing[ticker]:
            rows.setdefault(old["date"], {
                "Date": pd.Timestamp(old["date"]), "Close": old["close"],
                "Volume": old["volume"], "pct_change": old["pct_change"],
            })
        df = pd.DataFrame([rows[d] for d in sorted(rows)])
        df["avg_volume_20d"] = df["Volume"].rolling(20, min_periods=20).mean()
        histories[ticker] = df
    return histories
