import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    volume INTEGER,
    avg_volume_20d REAL,
    pct_change REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    company TEXT,
    exchange TEXT,
    published_at TEXT,
    headline TEXT,
    category TEXT,
    priority TEXT,
    why_it_matters TEXT,
    attachment_url TEXT,
    source TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_state (
    feed TEXT PRIMARY KEY,
    attempted_at TEXT,
    succeeded_at TEXT,
    error TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);

CREATE TABLE IF NOT EXISTS research_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
"""


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Older builds treated NSE's Indian timestamps as UTC. Correct those
        # stored rows once so identical RSS/API filings also deduplicate.
        if not conn.execute("SELECT 1 FROM migrations WHERE name = 'nse_ist'").fetchone():
            conn.execute("""
                UPDATE announcements
                SET published_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', published_at, '-330 minutes')
                WHERE source = 'nse_live' AND published_at IS NOT NULL
            """)
            conn.execute("INSERT INTO migrations VALUES ('nse_ist')")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
