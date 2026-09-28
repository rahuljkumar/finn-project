"""NSE corporate announcements — unofficial public JSON endpoint.

NSE requires a warmed-up browser-like session (cookies set by an initial
page load) before its /api/* endpoints will respond with JSON instead of
a block page. This is best-effort: NSE may rate-limit or block requests
from non-browser / datacenter IPs at any time, which is exactly why the
digest/alerts pipeline always has a seed-data fallback.
"""

import logging
import time
from datetime import datetime, timedelta

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


def fetch_announcements(symbol: str, index: str = "equities", days: int = 120) -> list[dict]:
    """Fetch corporate announcements for an NSE symbol over the last `days`
    days (the endpoint returns years of history if unfiltered). Returns []
    on any failure (blocked, rate-limited, schema change) rather than raising —
    callers should fall back to seed data."""
    try:
        s = _session()
        to_date = datetime.now().strftime("%d-%m-%Y")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")
        resp = s.get(
            f"{BASE}{ANNOUNCEMENTS_PATH}",
            params={
                "index": index,
                "symbol": symbol,
                "from_date": from_date,
                "to_date": to_date,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        logger.warning("NSE fetch failed for %s: %s", symbol, e)
        return []
