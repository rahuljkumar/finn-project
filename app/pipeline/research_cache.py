"""Persist successful research output and bound simultaneous RAG memory use."""

import json
import logging
import math
import threading
from datetime import datetime, timezone

from app.config import REASONING_MODEL
from app.db import get_conn

logger = logging.getLogger(__name__)
_slot = threading.Lock()


class ResearchBusy(RuntimeError):
    pass


def _prefix(ticker, category=None):
    prefix = f"v2:{REASONING_MODEL}:{ticker}:"
    return f"{prefix}{category}:" if category else prefix


def _saved_rows(prefix):
    # Topic names contain underscores, so quote LIKE's metacharacters rather
    # than accidentally treating them as wildcards for another topic.
    pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM research_cache WHERE cache_key LIKE ? ESCAPE '\\' ORDER BY saved_at DESC",
            (pattern,),
        ).fetchall()


def _valid_payload(data, category):
    """Check the stored shape before passing untrusted cache data to Jinja."""
    if category != "promised_vs_delivered":
        return (
            isinstance(data, dict)
            and isinstance(data.get("content"), str)
            and isinstance(data.get("sources"), list)
            and all(isinstance(source, dict)
                    and isinstance(source.get("label"), str)
                    and (source.get("url") is None or isinstance(source["url"], str))
                    for source in data["sources"])
        )
    if not isinstance(data, list):
        return False
    for quarter in data:
        if (not isinstance(quarter, dict)
                or not all(isinstance(quarter.get(name), str) for name in ("quarter", "verdict", "rationale"))
                or not isinstance(quarter.get("actual"), dict)
                or any(quarter.get(name) is not None and not isinstance(quarter[name], str)
                       for name in ("guidance_source", "guidance_source_url"))):
            return False
        for metric in ("revenue", "ebitda", "pbt", "pat"):
            actual = quarter["actual"].get(metric)
            if not isinstance(actual, dict) or "qoq_pct" not in actual:
                return False
            value = actual["qoq_pct"]
            if value is not None:
                if type(value) not in (int, float):
                    return False
                try:
                    if not math.isfinite(value):
                        return False
                except OverflowError:
                    return False
    return True


def _result(row, category, *, stale):
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError, RecursionError):
        return None
    if not _valid_payload(data, category):
        return None
    return {"data": data, "saved_at": row["saved_at"], "stale": stale}


def saved_research(ticker, category, *, revision=None):
    """Read current research, or the newest previous answer during a refresh."""
    if revision is None:
        from app.pipeline.pull_pipeline import corpus_revision
        revision = corpus_revision(ticker)
    prefix = _prefix(ticker, category)
    key = f"{prefix}{revision}"
    rows = _saved_rows(prefix)
    fallback = None
    for row in rows:
        current = row["cache_key"] == key
        result = _result(row, category, stale=not current)
        if result and current:
            return result
        if result and fallback is None:
            fallback = result
    return fallback


def saved_research_sections(ticker):
    """Read all nine cards without repeatedly inspecting the source corpus."""
    from app.pipeline.pull_pipeline import CATEGORY_PROMPTS, corpus_revision
    revision = corpus_revision(ticker)
    categories = set(CATEGORY_PROMPTS) | {"promised_vs_delivered"}
    prefix = _prefix(ticker)
    results = {}
    for row in _saved_rows(prefix):
        category, separator, row_revision = row["cache_key"][len(prefix):].partition(":")
        if not separator or category not in categories:
            continue
        current = row_revision == revision
        # Rows arrive newest first, but a current answer always takes
        # precedence over a newer timestamp from an older source revision.
        if category not in results or (current and results[category]["stale"]):
            result = _result(row, category, stale=not current)
            if result:
                results[category] = result
    return results


def _valid_output(data, category):
    if not _valid_payload(data, category):
        return False
    if isinstance(data, dict):
        content = data.get("content", "")
        return bool(content) and not content.startswith(("Couldn't", "No primary-source"))
    if isinstance(data, list):
        # An empty quarterly comparison is a valid result for this source
        # revision; it becomes outdated when quarterly evidence changes.
        return all(not q.get("rationale", "").startswith("Couldn't") for q in data)
    return bool(data)


def cached_research(ticker, category, generate):
    from app.pipeline.pull_pipeline import corpus_revision
    revision = corpus_revision(ticker)
    saved = saved_research(ticker, category, revision=revision)
    # Research is valid for its evidence revision, irrespective of its age.
    if saved and not saved["stale"]:
        return saved
    # Cached answers bypass the slot. New answers need it before loading
    # large vector indexes; concurrent mobile clicks won't multiply RAM use.
    if not _slot.acquire(timeout=2):
        if saved:
            return {**saved, "stale": True}
        raise ResearchBusy("Another research request is being prepared. Retry this section shortly.")
    try:
        # The source or another answer may have changed while we waited.
        revision = corpus_revision(ticker)
        key = f"{_prefix(ticker, category)}{revision}"
        saved = saved_research(ticker, category, revision=revision)
        if saved and not saved["stale"]:
            return saved
        try:
            data = generate()
            if corpus_revision(ticker) != revision:
                if saved:
                    return {**saved, "stale": True}
                raise ResearchBusy("Source reports changed while this section was being prepared. Retry shortly.")
            if not _valid_output(data, category):
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
