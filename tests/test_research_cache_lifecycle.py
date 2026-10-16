import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from app.db import get_conn, init_db
from app.pipeline import research_cache as cache
from app.pipeline.pull_pipeline import CATEGORY_PROMPTS


class ResearchCacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target, value in (
            ("app.db.DB_PATH", str(Path(self.folder.name) / "test.db")),
            ("app.pipeline.research_cache.REASONING_MODEL", "test-model"),
            ("app.pipeline.research_cache._slot", threading.Lock()),
        ):
            setting = patch(target, value)
            setting.start()
            self.addCleanup(setting.stop)
        revision = patch("app.pipeline.pull_pipeline.corpus_revision", return_value="r1")
        self.revision = revision.start()
        self.addCleanup(revision.stop)
        init_db()

    def save(self, content="Saved answer", *, category="business_snapshot", revision="r1",
             saved_at="2000-01-01T00:00:00+00:00", ticker="AAA", model="test-model"):
        data = content if isinstance(content, list) else {"content": content, "sources": []}
        with get_conn() as conn:
            conn.execute("INSERT INTO research_cache VALUES (?, ?, ?)",
                         (f"v2:{model}:{ticker}:{category}:{revision}", json.dumps(data), saved_at))
        return data

    def save_raw(self, payload, *, category="business_snapshot", revision="r1"):
        with get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO research_cache VALUES (?, ?, ?)",
                         (f"v2:test-model:AAA:{category}:{revision}", payload, "2002-01-01T00:00:00+00:00"))

    def test_unchanged_answer_does_not_expire_with_age(self):
        data = self.save()
        generate = Mock(side_effect=AssertionError("A saved answer should not regenerate"))
        result = cache.cached_research("AAA", "business_snapshot", generate)
        generate.assert_not_called()
        self.assertEqual(result, {"data": data, "saved_at": "2000-01-01T00:00:00+00:00", "stale": False})

    def test_new_evidence_reads_latest_old_answer_without_generating(self):
        self.save("Older answer", revision="r0")
        data = self.save("Newest saved answer", revision="r1", saved_at="2001-01-01T00:00:00+00:00")
        self.save("Another company", ticker="BBB", revision="r1", saved_at="2002-01-01T00:00:00+00:00")
        self.save("Another model", model="other-model", revision="r1", saved_at="2002-01-01T00:00:00+00:00")
        self.save("Another topic", category="financial_snapshot", revision="r1", saved_at="2002-01-01T00:00:00+00:00")
        self.revision.return_value = "r2"
        result = cache.saved_research("AAA", "business_snapshot")
        self.assertEqual(result["data"], data)
        self.assertTrue(result["stale"])

    def test_current_revision_takes_priority_over_newer_old_timestamp(self):
        data = self.save("Current answer", revision="r1")
        self.save("Older source answer", revision="r0", saved_at="2001-01-01T00:00:00+00:00")
        result = cache.saved_research("AAA", "business_snapshot")
        self.assertEqual(result["data"], data)
        self.assertFalse(result["stale"])

    def test_failed_refresh_keeps_previous_answer(self):
        data = self.save()
        self.revision.return_value = "r2"
        with self.assertLogs(cache.logger, level="WARNING"):
            result = cache.cached_research("AAA", "business_snapshot", Mock(side_effect=RuntimeError("Offline")))
        self.assertEqual(result["data"], data)
        self.assertTrue(result["stale"])
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0], 1)

    def test_changed_evidence_refreshes_once_and_reuses_new_answer(self):
        self.save()
        self.revision.return_value = "r2"
        generate = Mock(return_value={"content": "Updated answer", "sources": []})
        first = cache.cached_research("AAA", "business_snapshot", generate)
        second = cache.cached_research("AAA", "business_snapshot", generate)
        self.assertEqual(first, second)
        self.assertFalse(second["stale"])
        self.assertEqual(second["data"]["content"], "Updated answer")
        generate.assert_called_once()

    def test_saved_answer_bypasses_busy_generation_slot(self):
        data = self.save()
        slot = Mock()
        slot.acquire.side_effect = AssertionError("Warm requests must not wait for generation")
        with patch.object(cache, "_slot", slot):
            result = cache.cached_research("AAA", "business_snapshot", Mock())
        self.assertEqual(result["data"], data)
        slot.acquire.assert_not_called()

    def test_changed_evidence_during_generation_does_not_publish_answer(self):
        def changed_source():
            self.revision.return_value = "r2"
            return {"content": "Answer spanning changing sources", "sources": []}

        with self.assertRaises(cache.ResearchBusy):
            cache.cached_research("AAA", "business_snapshot", changed_source)
        self.assertIsNone(cache.saved_research("AAA", "business_snapshot"))
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0], 0)

    def test_changed_evidence_during_generation_retains_previous_answer(self):
        data = self.save(revision="r0")

        def changed_source():
            self.revision.return_value = "r2"
            return {"content": "Answer spanning changing sources", "sources": []}

        result = cache.cached_research("AAA", "business_snapshot", changed_source)
        self.assertEqual(result["data"], data)
        self.assertTrue(result["stale"])
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0], 1)

    def test_empty_quarter_comparison_is_reused_for_unchanged_sources(self):
        generate = Mock(return_value=[])
        first = cache.cached_research("AAA", "promised_vs_delivered", generate)
        second = cache.cached_research("AAA", "promised_vs_delivered", generate)
        self.assertEqual(first, second)
        self.assertEqual(second["data"], [])
        self.assertIsNotNone(second["saved_at"])
        generate.assert_called_once()

    def test_unavailable_generation_does_not_replace_success(self):
        data = self.save()
        self.revision.return_value = "r2"
        result = cache.cached_research("AAA", "business_snapshot", lambda: {"content": "Couldn't generate this now."})
        self.assertEqual(result["data"], data)
        self.assertTrue(result["stale"])
        self.assertEqual(cache.saved_research("AAA", "business_snapshot")["data"], data)

    def test_bulk_read_has_all_nine_topics_and_one_source_revision_lookup(self):
        categories = [*CATEGORY_PROMPTS, "promised_vs_delivered"]
        for category in categories:
            self.save([] if category == "promised_vs_delivered" else category, category=category)
        self.save("Outdated snapshot", category="business_snapshot", revision="r0",
                  saved_at="2001-01-01T00:00:00+00:00")
        statements = []

        @contextmanager
        def traced_connection():
            with get_conn() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        self.revision.reset_mock()
        with patch.object(cache, "get_conn", traced_connection):
            results = cache.saved_research_sections("AAA")
        self.assertEqual(set(results), set(categories))
        self.assertEqual(results["business_snapshot"]["data"]["content"], "business_snapshot")
        self.assertTrue(all(not result["stale"] for result in results.values()))
        self.assertEqual(results["promised_vs_delivered"]["data"], [])
        self.revision.assert_called_once_with("AAA")
        self.assertEqual(sum("SELECT" in statement and "research_cache" in statement for statement in statements), 1)

    def test_bulk_read_keeps_old_answers_marked_stale(self):
        self.save(category="business_snapshot", revision="r0")
        self.save(category="guidance", revision="r1")
        results = cache.saved_research_sections("AAA")
        self.assertTrue(results["business_snapshot"]["stale"])
        self.assertFalse(results["guidance"]["stale"])
        self.assertNotIn("trajectory", results)

    def test_corrupt_current_json_falls_back_to_previous_valid_answer(self):
        data = self.save(revision="r0")
        self.save_raw("{broken JSON")
        for result in (cache.saved_research("AAA", "business_snapshot"),
                       cache.saved_research_sections("AAA")["business_snapshot"]):
            self.assertEqual(result["data"], data)
            self.assertTrue(result["stale"])

    def test_corrupt_saved_data_without_a_fallback_is_missing(self):
        self.save_raw("{broken JSON")
        self.assertIsNone(cache.saved_research("AAA", "business_snapshot"))
        self.assertNotIn("business_snapshot", cache.saved_research_sections("AAA"))

    def test_wrong_section_shapes_fall_back_to_previous_valid_answer(self):
        data = self.save(revision="r0")
        for malformed in ([], "Answer", None, {"content": 123, "sources": []},
                          {"content": "Answer", "sources": {}},
                          {"content": "Answer", "sources": ["Annual Report"]},
                          {"content": "Answer", "sources": [{"label": "Report", "url": []}]}):
            with self.subTest(malformed=malformed):
                self.save_raw(json.dumps(malformed))
                saved = cache.saved_research("AAA", "business_snapshot")
                bulk = cache.saved_research_sections("AAA")
                self.assertEqual(saved["data"], data)
                self.assertTrue(saved["stale"])
                self.assertEqual(bulk["business_snapshot"], saved)

    def test_malformed_quarter_comparisons_fall_back_to_previous_valid_answer(self):
        quarter = {"quarter": "Jun 2026", "verdict": "unclear", "rationale": "No explicit guidance.",
                   "actual": {metric: {"qoq_pct": None} for metric in ("revenue", "ebitda", "pbt", "pat")}}
        data = self.save([quarter], category="promised_vs_delivered", revision="r0")
        malformed_rows = [
            {"content": "Wrong section shape", "sources": []},
            [None],
            [{"quarter": "Jun 2026", "rationale": "No actual numbers"}],
            [{**quarter, "actual": {}}],
            [{**quarter, "actual": {**quarter["actual"], "pat": {"qoq_pct": "ten percent"}}}],
            [{**quarter, "rationale": ["Wrong rationale type"]}],
            [{**quarter, "guidance_source_url": ["Wrong URL type"]}],
        ]
        for malformed in malformed_rows:
            with self.subTest(malformed=malformed):
                self.save_raw(json.dumps(malformed), category="promised_vs_delivered")
                saved = cache.saved_research("AAA", "promised_vs_delivered")
                bulk = cache.saved_research_sections("AAA")
                self.assertEqual(saved["data"], data)
                self.assertTrue(saved["stale"])
                self.assertEqual(bulk["promised_vs_delivered"], saved)

    def test_successful_generation_repairs_a_corrupt_current_answer(self):
        self.save_raw("null")
        data = {"content": "New valid answer", "sources": []}
        result = cache.cached_research("AAA", "business_snapshot", lambda: data)
        self.assertEqual(result["data"], data)
        self.assertFalse(result["stale"])
        self.assertEqual(cache.saved_research("AAA", "business_snapshot"), result)


if __name__ == "__main__":
    unittest.main()
