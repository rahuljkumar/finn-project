"""Small background executor and persisted results for the single-process demo."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.config import OPENAI_API_KEY
from app.db import get_conn

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="finn-ai")
_guard = threading.Lock()
_running: set[str] = set()


def get_job(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ai_jobs WHERE job_key = ?", (key,)).fetchone()
    if not row:
        return None
    job = dict(row)
    payload = job.pop("payload")
    job["result"] = json.loads(payload) if payload else None
    return job


def _run(key, work):
    try:
        result = work()
        with get_conn() as conn:
            conn.execute("UPDATE ai_jobs SET state='ready', payload=?, error=NULL, updated_at=? WHERE job_key=?",
                         (json.dumps(result), datetime.now(timezone.utc).isoformat(), key))
    except Exception:
        logger.exception("Background task failed: %s", key)
        with get_conn() as conn:
            conn.execute("UPDATE ai_jobs SET state='error', error=?, updated_at=? WHERE job_key=?",
                         ("Couldn't prepare this right now. Retry shortly; saved sources are retained.",
                          datetime.now(timezone.utc).isoformat(), key))
    finally:
        with _guard:
            _running.discard(key)


def start_job(key: str, kind: str, work, *, max_age: int | None = None,
              retry: bool = False, requires_model: bool = True) -> dict:
    if requires_model and not OPENAI_API_KEY:
        return {"job_key": key, "kind": kind, "state": "disabled", "result": None,
                "error": "Add your OpenAI API key to enable AI answers."}
    now = datetime.now(timezone.utc)
    with _guard:
        saved = get_job(key)
        if key in _running:
            return saved
        if saved:
            age = (now - datetime.fromisoformat(saved["updated_at"])).total_seconds()
            if saved["state"] == "ready" and (max_age is None or age < max_age):
                return saved
            if saved["state"] == "error" and not retry and age < 900:
                return saved
        # A pending row without a local worker is from a previous app process.
        with get_conn() as conn:
            conn.execute("""INSERT INTO ai_jobs (job_key,kind,state,updated_at) VALUES (?,?,'running',?)
                ON CONFLICT(job_key) DO UPDATE SET state='running', error=NULL, updated_at=excluded.updated_at""",
                         (key, kind, now.isoformat()))
        _running.add(key)
        _pool.submit(_run, key, work)
    return get_job(key)
