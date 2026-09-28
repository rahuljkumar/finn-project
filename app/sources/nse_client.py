"""NSE corporate announcements — unofficial public JSON endpoint.

NSE requires a warmed-up browser-like session (cookies set by an initial
page load) before its /api/* endpoints will respond with JSON instead of
a block page. This is best-effort: NSE may rate-limit or block requests
from non-browser / datacenter IPs at any time. The live digest uses official
RSS as its primary feed and retains saved data when both feeds are unavailable.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
ANNOUNCEMENTS_PATH = "/api/corporate-announcements"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # Warm-up: load a normal page first so NSE sets session cookies.
    s.get(BASE, timeout=10)
    time.sleep(0.5)
    s.get(f"{BASE}/companies-listing/corporate-filings-announcements", timeout=10)
    return s


def fetch_announcements(symbol: str | list[str] | None = None, index: str = "equities", days: int = 120) -> list[dict]:
    """Fetch corporate announcements for an NSE symbol over the last `days`
    days (the endpoint returns years of history if unfiltered). Returns []
    on any failure (blocked, rate-limited, schema change) rather than raising —
    callers should retain saved live data."""
    try:
        s = _session()
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        to_date = now.strftime("%d-%m-%Y")
        from_date = (now - timedelta(days=days)).strftime("%d-%m-%Y")
        params = {"index": index, "from_date": from_date, "to_date": to_date}
        symbols = symbol if isinstance(symbol, list) else [symbol]
        items = []
        # Reuse one session, request only portfolio companies, and abort on
        # the first block. An all-market 120-day response can be very large.
        for offset, ticker in enumerate(symbols):
            if offset:
                time.sleep(0.35)
            query = {**params, "symbol": ticker} if ticker else params
            resp = s.get(f"{BASE}{ANNOUNCEMENTS_PATH}", params=query, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data if isinstance(data, list) else data.get("data", []))
        return items
    except Exception as e:
        logger.warning("NSE fetch failed for %s: %s", symbol, e)
        return []
