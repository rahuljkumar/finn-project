"""Recent filings must remain usable while optional maintenance is slow."""

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import feeds
from app.db import get_conn, init_db
from app.main import app
from app.pipeline import digest
from test_support import isolate_background


REQUEST_REFRESH = feeds.request_refresh
CLASSIFY_BATCH = digest.classify_batch
COMPANIES = [{"ticker": "AAA", "name": "Alpha"}]
PORTFOLIO = {"portfolio": COMPANIES, "adhoc": []}


def filing(number, desc="Acquisition", *, age=0):
    return {"symbol": "AAA", "desc": desc,
            "attchmntText": f"Alpha disclosure {number}",
            "attchmntFile": f"https://example.com/filing-{number}.pdf",
            "sort_date": (datetime.now(timezone.utc) - timedelta(minutes=age)).isoformat()}


class DigestRefreshTests(unittest.TestCase):
    def setUp(self):
        isolate_background(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.patch("app.db.DB_PATH", str(Path(folder.name) / "digest.db"))
        init_db()
        self.patch("app.feeds._running", set())
        self.patch("app.config.USE_LIVE_NSE", True)
        self.patch("app.pipeline.digest.load_portfolio", return_value=PORTFOLIO)
        self.patch("app.pipeline.digest.USE_LIVE_NSE", True)
        self.rss = self.patch("app.pipeline.digest.fetch_recent_announcements", return_value=[])
        self.history = self.patch("app.pipeline.digest.fetch_announcements", return_value=[])
        self.search = self.patch("app.pipeline.digest.fetch_search_announcements",
                                 side_effect=AssertionError("Unexpected web discovery"))
        self.model = self.patch("app.pipeline.digest.classify_batch",
                                side_effect=AssertionError("Unexpected model classification"))
        self.prepare = self.patch("app.pipeline.summaries.prepare_presets")

    def patch(self, target, *args, **kwargs):
        mocked = patch(target, *args, **kwargs)
        value = mocked.start()
        self.addCleanup(mocked.stop)
        return value

    def rows(self):
        with get_conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM announcements")]

    def has_backfill(self):
        with get_conn() as conn:
            return conn.execute("SELECT 1 FROM migrations WHERE name='nse_backfill'").fetchone() is not None

    def test_existing_database_migrates_without_losing_saved_filings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "legacy.db")
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("""CREATE TABLE announcements (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, company TEXT, exchange TEXT,
                    published_at TEXT, headline TEXT, category TEXT, priority TEXT,
                    why_it_matters TEXT, attachment_url TEXT, source TEXT, raw_json TEXT)""")
                conn.execute("""INSERT INTO announcements (id,ticker,published_at,headline,category,priority,source)
                    VALUES ('saved','AAA',?,'Previously saved filing','M&A','high','nse_rss')""",
                    (datetime.now(timezone.utc).isoformat(),))
            with patch("app.db.DB_PATH", path):
                init_db()
                init_db()
                self.assertEqual(self.rows()[0]["headline"], "Previously saved filing")
                self.assertIsNone(self.rows()[0]["classification_input"])
                self.assertEqual(digest.get_digest()["high"][0]["id"], "saved")

    def test_recent_refresh_saves_rules_and_pending_without_history_or_model(self):
        self.rss.return_value = [filing("known"), filing("pending", "General Updates")]

        self.assertEqual(digest.refresh_all(), 2)
        self.assertEqual(digest.refresh_all(), 0)

        self.history.assert_not_called()
        self.model.assert_not_called()
        self.search.assert_not_called()
        self.assertFalse(self.has_backfill())
        rows = {row["headline"]: row for row in self.rows()}
        known = rows["Alpha disclosure known"]
        pending = rows["Alpha disclosure pending"]
        self.assertEqual((known["category"], known["priority"]), ("M&A", "high"))
        self.assertIsNone(known["classification_input"])
        self.assertEqual(pending["category"], "unclassified")
        original = json.loads(pending["classification_input"])
        self.assertEqual(original["desc"], "General Updates")
        self.assertEqual(original["seq_id"], pending["id"])

    def test_empty_successful_rss_does_not_start_historical_api(self):
        self.rss.return_value = []
        self.assertEqual(digest.refresh_all(), 0)
        self.history.assert_not_called()
        self.search.assert_not_called()

    def test_api_fallback_is_short_and_does_not_mark_history_complete(self):
        self.rss.return_value = None
        self.history.return_value = [filing("fallback")]

        self.assertEqual(digest.refresh_all(), 1)

        self.assertEqual(self.history.call_args.kwargs["days"], 2)
        self.assertFalse(self.has_backfill())
        self.model.assert_not_called()

    def test_saved_filings_and_http_page_remain_available_during_blocked_classification(self):
        self.rss.return_value = [filing("known"), filing("pending", "General Updates")]
        self.assertEqual(digest.refresh_all(), 2)
        entered, release = threading.Event(), threading.Event()

        def classify(items):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release classifier")
            return {item["seq_id"]: "M&A" for item in items}

        self.model.side_effect = classify
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(digest.classify_pending_announcements)
            try:
                self.assertTrue(entered.wait(2))
                # Read requests and source writes must not wait for the model.
                response = TestClient(app).get("/digest")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Alpha disclosure known", response.text)
                self.assertIn("Alpha disclosure pending", response.text)
                self.rss.return_value.append(filing("another-known"))
                self.assertEqual(digest.refresh_all(), 1)
                self.assertEqual(len(self.rows()), 3)
                pending = next(row for row in self.rows() if row["headline"] == "Alpha disclosure pending")
                self.assertIsNotNone(pending["classification_input"])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2), 1)
        self.assertTrue(all(row["classification_input"] is None for row in self.rows()))

    def test_recent_refresh_completes_while_historical_source_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        historical = filing("historical", age=2000)

        def fetch(tickers, days):
            self.assertEqual(days, 120)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release historical source")
            return [historical]

        self.history.side_effect = fetch
        self.rss.return_value = [filing("recent")]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(digest.backfill_filings)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(digest.refresh_all(), 1)
                self.assertEqual([row["headline"] for row in self.rows()], ["Alpha disclosure recent"])
                self.assertFalse(self.has_backfill())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2), 1)
        self.assertTrue(self.has_backfill())
        self.model.assert_not_called()

    def test_historical_backfill_deduplicates_recent_and_marks_only_full_success(self):
        shared = filing("shared")
        self.rss.return_value = [shared]
        self.assertEqual(digest.refresh_all(), 1)
        self.assertFalse(self.has_backfill())

        with self.assertRaises(RuntimeError):
            digest.backfill_filings()
        self.assertFalse(self.has_backfill())
        self.assertEqual(len(self.rows()), 1)

        self.history.return_value = [shared, filing("old", "General Updates", age=2000)]
        self.assertEqual(digest.backfill_filings(), 1)
        self.assertTrue(self.has_backfill())
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.history.call_args.kwargs["days"], 120)
        self.assertIsNotNone(next(row for row in self.rows() if row["headline"] == "Alpha disclosure old")["classification_input"])
        self.model.assert_not_called()

    def test_pending_classification_survives_database_reinitialization_and_retry(self):
        self.rss.return_value = [filing("pending", "General Updates")]
        self.assertEqual(digest.refresh_all(), 1)
        original = self.rows()[0]["classification_input"]
        init_db()
        self.assertEqual(self.rows()[0]["classification_input"], original)

        self.model.side_effect = RuntimeError("Classifier unavailable")
        with self.assertRaises(RuntimeError):
            digest.classify_pending_announcements()
        self.assertEqual(self.rows()[0]["classification_input"], original)

        init_db()
        self.model.side_effect = lambda items: {item["seq_id"]: "dividend" for item in items}
        self.assertEqual(digest.classify_pending_announcements(), 1)
        self.assertEqual(digest.classify_pending_announcements(), 0)
        row = self.rows()[0]
        self.assertEqual((row["category"], row["priority"]), ("dividend", "medium"))
        self.assertIsNone(row["classification_input"])

    def test_pending_classifier_batches_newest_first(self):
        self.rss.return_value = [filing(index, "General Updates", age=index) for index in range(41)]
        self.assertEqual(digest.refresh_all(), 41)
        batches = []

        def classify(items):
            batches.append([item["attchmntText"] for item in items])
            return {item["seq_id"]: "unclassified" for item in items}

        self.model.side_effect = classify
        self.assertEqual(digest.classify_pending_announcements(), 41)
        self.assertEqual([len(batch) for batch in batches], [40, 1])
        self.assertEqual(batches[0][0], "Alpha disclosure 0")
        self.assertEqual(batches[1][0], "Alpha disclosure 40")
        self.assertTrue(all(row["classification_input"] is None for row in self.rows()))

    def test_provider_classification_failure_preserves_pending_inputs(self):
        self.rss.return_value = [filing("pending", "General Updates")]
        self.assertEqual(digest.refresh_all(), 1)
        original = self.rows()[0]["classification_input"]
        with patch.object(digest, "classify_batch", side_effect=CLASSIFY_BATCH), \
             patch("app.pipeline.classify.chat_json", side_effect=RuntimeError("Provider unavailable")), \
             self.assertLogs("app.pipeline.classify", level="WARNING"), \
             self.assertRaises(RuntimeError):
            digest.classify_pending_announcements()
        self.assertEqual(self.rows()[0]["classification_input"], original)
        init_db()
        self.assertEqual(self.rows()[0]["classification_input"], original)

    def test_scheduler_skips_completed_backfill_and_empty_classification_queue(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO migrations VALUES ('nse_backfill')")
        with patch.object(feeds.threading, "Thread") as thread:
            self.assertFalse(REQUEST_REFRESH("filings_backfill", force=True))
            self.assertFalse(REQUEST_REFRESH("filings_classify", force=True))
        thread.assert_not_called()

    def test_scheduler_allows_another_recent_refresh_while_history_is_blocked(self):
        historical_entered, release_history = threading.Event(), threading.Event()
        historical_finished = threading.Event()
        source_finished = [threading.Event(), threading.Event()]
        source_calls = []
        original_job = feeds._job

        def historical(tickers, days):
            self.assertEqual(days, 120)
            historical_entered.set()
            if not release_history.wait(5):
                raise RuntimeError("Test did not release historical source")
            return [filing("old", age=2000)]

        def observed_job(feed):
            try:
                original_job(feed)
            finally:
                if feed == "filings":
                    source_calls.append(feed)
                    source_finished[len(source_calls) - 1].set()
                elif feed == "filings_backfill":
                    historical_finished.set()

        self.rss.return_value = [filing("first")]
        self.history.side_effect = historical
        with patch.object(feeds, "request_refresh", side_effect=REQUEST_REFRESH), \
             patch.object(feeds, "_job", side_effect=observed_job):
            try:
                self.assertTrue(REQUEST_REFRESH("filings"))
                self.assertTrue(historical_entered.wait(2))
                self.assertTrue(source_finished[0].wait(2))
                self.assertFalse(feeds.feed_status("filings")["running"])
                self.assertTrue(feeds.feed_status("filings")["backfill_running"])
                # Expire the normal source cooldown; maintenance remains blocked.
                with get_conn() as conn:
                    conn.execute("UPDATE feed_state SET attempted_at=? WHERE feed='filings'",
                                 ((datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),))
                self.rss.return_value.append(filing("second"))
                self.assertTrue(REQUEST_REFRESH("filings"))
                self.assertTrue(source_finished[1].wait(2))
                status = feeds.feed_status("filings")
                self.assertIsNotNone(status["succeeded_at"])
                self.assertFalse(status["running"])
                self.assertTrue(status["backfill_running"])
                self.assertEqual(status["summary"]["added"], 1)
                self.assertEqual(len(self.rows()), 2)
                self.assertEqual(self.history.call_count, 1)
            finally:
                release_history.set()
                self.assertTrue(historical_finished.wait(2))
        self.assertTrue(self.has_backfill())
        self.assertEqual(len(self.rows()), 3)
        self.model.assert_not_called()

    def test_historical_scheduler_retries_failed_backfill_only_after_daily_cooldown(self):
        finished = threading.Event()
        original_job = feeds._job

        def observed_job(feed):
            try:
                original_job(feed)
            finally:
                finished.set()

        with patch.object(feeds, "request_refresh", side_effect=REQUEST_REFRESH), \
             patch.object(feeds, "_job", side_effect=observed_job), \
             patch.object(feeds.logger, "exception"):
            self.assertTrue(REQUEST_REFRESH("filings_backfill"))
            self.assertTrue(finished.wait(2))
            self.assertFalse(self.has_backfill())
            self.assertIsNotNone(feeds.feed_status("filings_backfill")["error"])
            self.assertFalse(REQUEST_REFRESH("filings_backfill"))
            self.assertEqual(self.history.call_count, 1)
            with get_conn() as conn:
                conn.execute("UPDATE feed_state SET attempted_at=? WHERE feed='filings_backfill'",
                             ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),))
            finished.clear()
            self.assertTrue(REQUEST_REFRESH("filings_backfill"))
            self.assertTrue(finished.wait(2))
        self.assertEqual(self.history.call_count, 2)
        self.assertFalse(self.has_backfill())

    def test_classifier_scheduler_runs_new_pending_rows_after_just_completed_batch(self):
        finished = threading.Event()
        original_job = feeds._job

        def observed_job(feed):
            try:
                original_job(feed)
            finally:
                finished.set()

        self.rss.return_value = [filing("first", "General Updates")]
        self.assertEqual(digest.refresh_all(), 1)
        self.model.side_effect = lambda items: {item["seq_id"]: "dividend" for item in items}
        with patch.object(feeds, "request_refresh", side_effect=REQUEST_REFRESH), \
             patch.object(feeds, "_job", side_effect=observed_job):
            self.assertTrue(REQUEST_REFRESH("filings_classify"))
            self.assertTrue(finished.wait(2))
            self.assertIsNotNone(feeds.feed_status("filings_classify")["succeeded_at"])
            self.assertFalse(REQUEST_REFRESH("filings_classify"))
            self.rss.return_value.append(filing("second", "General Updates"))
            self.assertEqual(digest.refresh_all(), 1)
            finished.clear()
            # A new row must not wait for the five-minute maintenance interval.
            self.assertTrue(REQUEST_REFRESH("filings_classify"))
            self.assertTrue(finished.wait(2))
        self.assertEqual(self.model.call_count, 2)
        self.assertTrue(all(row["classification_input"] is None for row in self.rows()))

    def test_classifier_reschedules_input_arriving_after_queue_drains_before_worker_finishes(self):
        preparing, release_prepare = threading.Event(), threading.Event()
        finished = [threading.Event(), threading.Event()]
        worker_calls, prepare_calls = [], []
        worker_guard = threading.Lock()
        original_job = feeds._job

        def prepare():
            prepare_calls.append(True)
            if len(prepare_calls) == 1:
                preparing.set()
                if not release_prepare.wait(5):
                    raise RuntimeError("Test did not release preset preparation")

        def observed_job(feed):
            with worker_guard:
                index = len(worker_calls)
                worker_calls.append(feed)
            try:
                original_job(feed)
            finally:
                finished[index].set()

        self.rss.return_value = [filing("first", "General Updates")]
        self.assertEqual(digest.refresh_all(), 1)
        self.model.side_effect = lambda items: {item["seq_id"]: "dividend" for item in items}
        self.prepare.side_effect = prepare
        with patch.object(feeds, "request_refresh", side_effect=REQUEST_REFRESH), \
             patch.object(feeds, "_job", side_effect=observed_job):
            try:
                self.assertTrue(REQUEST_REFRESH("filings_classify"))
                self.assertTrue(preparing.wait(2))
                self.assertTrue(all(row["classification_input"] is None for row in self.rows()))
                self.rss.return_value.append(filing("second", "General Updates"))
                self.assertEqual(digest.refresh_all(), 1)
                # The producer observes the old worker still registered, even
                # though that worker's classification loop has already exited.
                self.assertFalse(REQUEST_REFRESH("filings_classify"))
                self.assertTrue(feeds.feed_status("filings")["classification_running"])
            finally:
                release_prepare.set()
                self.assertTrue(finished[0].wait(2))
            self.assertTrue(finished[1].wait(2))
        self.assertEqual(worker_calls, ["filings_classify", "filings_classify"])
        self.assertEqual(self.model.call_count, 2)
        self.assertTrue(all(row["classification_input"] is None for row in self.rows()))

    def test_source_status_finishes_while_maintenance_still_polls(self):
        with feeds._guard:
            feeds._running.update({"filings_backfill", "filings_classify"})
        status = feeds.feed_status("filings")
        self.assertFalse(status["running"])
        self.assertTrue(status["polling"])
        self.assertTrue(status["classification_running"])
        self.assertTrue(status["backfill_running"])
        response = TestClient(app).get("/digest")
        self.assertEqual(response.status_code, 200)
        self.assertIn('hx-trigger="every 2s"', response.text)
        self.assertIn('hx-select="#digest-results"', response.text)
        self.assertNotIn("Refreshing data", response.text)


if __name__ == "__main__":
    unittest.main()
