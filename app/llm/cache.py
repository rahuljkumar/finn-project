"""Content-hash-keyed cache for LLM responses, backed by the llm_cache
SQLite table. Avoids re-calling the API for repeat views (digest reload,
re-testing during the video recording) and protects the API budget."""

from datetime import datetime, timezone

from app.db import get_conn


def get_cached(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT response FROM llm_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    return row["response"] if row else None


def set_cached(key: str, response: str, model: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO llm_cache (cache_key, response, model, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response=excluded.response,
                model=excluded.model,
                created_at=excluded.created_at
            """,
            (key, response, model, datetime.now(timezone.utc).isoformat()),
        )
