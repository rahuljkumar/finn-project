import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app import jobs
from app.db import init_db
from app.pipeline import summaries


class JobPriorityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        db_patch = patch("app.db.DB_PATH", str(Path(self.folder.name) / "jobs.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        init_db()

    def test_interactive_job_finishes_while_background_worker_and_queue_are_busy(self):
        entered, release = threading.Event(), threading.Event()
        second_entered, interactive_finished = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(5)
            return {"content": "Background summary"}

        def queued():
            second_entered.set()
            return {"content": "Second background summary"}

        def interactive():
            interactive_finished.set()
            return {"content": "User answer"}

        with ThreadPoolExecutor(max_workers=2) as interactive_pool, \
             ThreadPoolExecutor(max_workers=1) as background_pool, \
             patch.object(jobs, "_pool", interactive_pool), \
             patch.object(jobs, "_background_pool", background_pool):
            try:
                jobs.start_job("priority:background", "digest", blocked,
                               background=True, requires_model=False)
                self.assertTrue(entered.wait(2))
                jobs.start_job("priority:queued", "digest", queued,
                               background=True, requires_model=False)
                jobs.start_job("priority:interactive", "conversation", interactive,
                               requires_model=False)
                self.assertTrue(interactive_finished.wait(2))
                # Waiting on the interactive pool also verifies durable completion.
                interactive_pool.shutdown(wait=True)
                self.assertEqual(jobs.get_job("priority:interactive")["state"], "ready")
                self.assertEqual(jobs.get_job("priority:background")["state"], "running")
                self.assertFalse(second_entered.is_set())
            finally:
                release.set()
        self.assertTrue(second_entered.is_set())

    def test_same_key_shares_running_job_and_cached_result_across_both_pools(self):
        entered, release = threading.Event(), threading.Event()
        original = Mock(side_effect=lambda: (entered.set(), release.wait(5), {"content": "Saved summary"})[-1])
        duplicate = Mock(return_value={"content": "Duplicate"})
        with ThreadPoolExecutor(max_workers=2) as interactive_pool, \
             ThreadPoolExecutor(max_workers=1) as background_pool, \
             patch.object(jobs, "_pool", interactive_pool), \
             patch.object(jobs, "_background_pool", background_pool):
            try:
                first = jobs.start_job("priority:shared", "digest", original,
                                       background=True, requires_model=False)
                self.assertTrue(entered.wait(2))
                second = jobs.start_job("priority:shared", "digest", duplicate,
                                        requires_model=False)
                self.assertEqual(first["job_key"], second["job_key"])
                self.assertEqual(second["state"], "running")
                duplicate.assert_not_called()
            finally:
                release.set()
        original.assert_called_once_with()
        for background in (False, True):
            cached = jobs.start_job("priority:shared", "digest", duplicate,
                                    background=background, requires_model=False)
            self.assertEqual(cached["state"], "ready")
            self.assertEqual(cached["result"], {"content": "Saved summary"})
        duplicate.assert_not_called()

    def test_automatic_summaries_use_background_and_explicit_generation_uses_interactive(self):
        item = {"id": "filing", "ticker": "AAA", "company": "Alpha", "headline": "Acquisition",
                "priority": "high", "category": "M&A", "attachment_url": "https://example.com/a.pdf",
                "published_at": datetime.now(timezone.utc).isoformat()}
        data = {"high": [item], "medium": [], "window_hours": 24}
        with patch.object(summaries, "start_job", return_value={"state": "running"}) as start:
            summaries.summary_groups(data)
            self.assertTrue(start.call_args.kwargs["background"])
            summaries.summary_groups(data, generate=True, retry=True)
            self.assertFalse(start.call_args.kwargs["background"])
            summaries.summary_groups(data, mode="custom", generate=True)
            self.assertFalse(start.call_args.kwargs["background"])


if __name__ == "__main__":
    unittest.main()
