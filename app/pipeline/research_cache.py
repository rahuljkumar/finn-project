"""Persist successful research output and bound simultaneous RAG memory use."""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from app.config import REASONING_MODEL
from app.db import get_conn

logger = logging.getLogger(__name__)
_slot = threading.Lock()
FRESH_FOR = timedelta(hours=6)


class ResearchBusy(RuntimeError):
    pass


def _saved(key):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM research_cache WHERE cache_key = ?", (key,)).fetchone()
    return {"data": json.loads(row["payload"]), "saved_at": row["saved_at"], "stale": False} if row else None


def cached_research(ticker, category, generate):
    key = f"v1:{REASONING_MODEL}:{ticker}:{category}"
    saved = _saved(key)
    if saved and datetime.now(timezone.utc) - datetime.fromisoformat(saved["saved_at"]) < FRESH_FOR:
        return saved
    # Cached answers bypass the slot. New answers need it before loading
    # large vector indexes; concurrent mobile clicks won't multiply RAM use.
    if not _slot.acquire(timeout=2):
        if saved:
            return {**saved, "stale": True}
        raise ResearchBusy("Another research request is being prepared. Retry this section shortly.")
    try:
        # A concurrent request may have completed while this one waited.
        latest = _saved(key)
        if latest and (not saved or latest["saved_at"] != saved["saved_at"]):
            return latest
        try:
            data = generate()
            valid = bool(data)
            if isinstance(data, dict):
                valid = bool(data.get("content")) and not data["content"].startswith(("Couldn't", "No primary-source"))
            elif isinstance(data, list):
                valid = valid and all(not q.get("rationale", "").startswith("Couldn't") for q in data)
            if not valid:
                if saved:
                    return {**saved, "stale": True}
                return {"data": data, "saved_at": None, "stale": False}
            at = datetime.now(timezone.utc).isoformat()
            with get_conn() as conn:
                conn.execute("""INSERT INTO research_cache VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, saved_at = excluded.saved_at""",
                             (key, json.dumps(data), at))
            return {"data": data, "saved_at": at, "stale": False}
        except Exception:
            if saved:
                logger.warning("Research refresh failed for %s/%s; serving saved answer", ticker, category)
                return {**saved, "stale": True}
            raise
    finally:
        _slot.release()
