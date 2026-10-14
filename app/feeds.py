"""Background refresh with one in-flight job per feed and durable health.

The service uses a single Uvicorn process. A paid always-on instance polls
without user traffic; free instances can only poll while Render keeps them up.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from app.db import get_conn
from app.config import DOCUMENT_SYNC_HOUR, DOCUMENT_SYNC_MINUTE

logger = logging.getLogger(__name__)
_guard = threading.Lock()
_running = set()
INTERVALS = {"filings": 300, "prices": 1800, "documents": 86400}


def feed_status(feed: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM feed_state WHERE feed = ?", (feed,)).fetchone()
    result = dict(row) if row else {"attempted_at": None, "succeeded_at": None, "error": None, "summary": None}
    result["summary"] = json.loads(result["summary"]) if result["summary"] else None
    with _guard:
        result["running"] = feed in _running
    return result


def _job(feed: str):
    from app.pipeline.alerts import refresh_price_history
    from app.pipeline.digest import refresh_all

    try:
        if feed == "filings":
            summary = {"added": refresh_all()}
        elif feed == "documents":
            from app.pipeline.pull_pipeline import sync_portfolio
            summary = sync_portfolio()
        else:
            summary = refresh_price_history()
        error = (("Some reports could not be indexed; saved reports are retained." if feed == "documents"
                  else "Some stock prices could not be refreshed; saved data is retained.") if summary.get("failed") else None)
        with get_conn() as conn:
            conn.execute("UPDATE feed_state SET succeeded_at = ?, error = ?, summary = ? WHERE feed = ?",
                         (datetime.now(timezone.utc).isoformat(), error, json.dumps(summary), feed))
        if feed == "filings":
            from app.pipeline.summaries import prepare_presets
            prepare_presets()
    except Exception:
        logger.exception("%s refresh failed", feed)
        # UI gets a useful fixed message; don't expose upstream response bodies.
        with get_conn() as conn:
            conn.execute("UPDATE feed_state SET error = ? WHERE feed = ?",
                         ("Live source unavailable. Saved data is retained; refresh will retry automatically.", feed))
    finally:
        with _guard:
            _running.discard(feed)


def request_refresh(feed: str, force: bool = False) -> bool:
    if feed not in INTERVALS:
        raise ValueError("Unknown feed")
    now = datetime.now(timezone.utc)
    with _guard:
        if feed in _running:
            return False
        with get_conn() as conn:
            row = conn.execute("SELECT attempted_at,error FROM feed_state WHERE feed = ?", (feed,)).fetchone()
            # Even manual clicks get a short cooldown to avoid provider bans.
            wait = 60 if force else INTERVALS[feed]
            if row and row[0]:
                attempted = datetime.fromisoformat(row[0])
                if feed == "documents" and not force:
                    if attempted >= document_schedule_slot(now) and (not row[1] or now - attempted < timedelta(hours=1)):
                        return False
                elif now - attempted < timedelta(seconds=wait):
                    return False
            conn.execute("""INSERT INTO feed_state (feed, attempted_at) VALUES (?, ?)
                            ON CONFLICT(feed) DO UPDATE SET attempted_at = excluded.attempted_at""",
                         (feed, now.isoformat()))
        _running.add(feed)
    threading.Thread(target=_job, args=(feed,), daemon=True, name=f"finn-{feed}").start()
    return True


def document_schedule_slot(now: datetime) -> datetime:
    ist = timezone(timedelta(hours=5, minutes=30))
    local = now.astimezone(ist)
    slot = local.replace(hour=DOCUMENT_SYNC_HOUR, minute=DOCUMENT_SYNC_MINUTE, second=0, microsecond=0)
    return (slot if local >= slot else slot - timedelta(days=1)).astimezone(timezone.utc)


def poll_feeds(stop: threading.Event):
    while not stop.is_set():
        for feed in INTERVALS:
            try:
                request_refresh(feed)
            except Exception:
                logger.exception("Could not schedule %s refresh", feed)
        stop.wait(30)
