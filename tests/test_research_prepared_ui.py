import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app
from app.pipeline import pull_pipeline as pull, research_cache as cache
from test_support import isolate_background


RUNNING_JOB = {"job_key": "research:AAA:test", "state": "running", "result": None, "error": None}
CATEGORIES = (*pull.CATEGORY_PROMPTS, "promised_vs_delivered")


class PreparedResearchUITests(unittest.TestCase):
    def setUp(self):
        isolate_background(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.indexes = self.folder / "indexes"
        self.indexes.mkdir()
        (self.indexes / "AAA.json").write_text('[{"text": "Saved report evidence"}]', encoding="utf-8")
        for target, value in (
            ("app.db.DB_PATH", str(self.folder / "test.db")),
            ("app.pipeline.pull_pipeline.INDEX_DIR", self.indexes),
            ("app.pipeline.research_cache.REASONING_MODEL", "test-model"),
            ("app.main.load_portfolio", lambda: {"portfolio": [{"ticker": "AAA", "name": "Acme"}], "adhoc": []}),
        ):
            setting = patch(target, value)
            setting.start()
            self.addCleanup(setting.stop)
        init_db()
        preparation = patch("app.main.request_research_preparation", return_value=None, create=True)
        self.prepare = preparation.start()
        self.addCleanup(preparation.stop)
        self.providers = []
        for name in ("generate_research_section", "chat_text", "embed", "retrieve"):
            provider = patch.object(pull, name, side_effect=AssertionError("HTTP views must use saved Research"))
            self.providers.append(provider.start())
            self.addCleanup(provider.stop)
        self.client = TestClient(app)

    def tearDown(self):
        for provider in self.providers:
            provider.assert_not_called()

    def save(self, category, *, content=None, revision=None, raw=None):
        if category == "promised_vs_delivered":
            data = [{"quarter": "Jun 2026", "verdict": "met", "rationale": content or "Saved quarterly comparison",
                     "actual": {metric: {"qoq_pct": None} for metric in ("revenue", "ebitda", "pbt", "pat")},
                     "guidance_source": "Mar 2026", "guidance_source_url": "https://example.com/call.pdf"}]
        else:
            data = {"content": content or f"Saved {category} answer", "sources": []}
        key = f"v2:test-model:AAA:{category}:{revision or pull.corpus_revision('AAA')}"
        with get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO research_cache VALUES (?, ?, ?)",
                         (key, raw if raw is not None else json.dumps(data), "2026-10-15T10:00:00+00:00"))
        return data

    @staticmethod
    def section_polls(response, category):
        soup = BeautifulSoup(response.text, "html.parser")
        return [element for element in soup.find_all(attrs={"hx-get": f"/research/AAA/section/{category}"})
                if "3s" in element.get("hx-trigger", "")]

    def test_company_page_contains_all_nine_saved_sections_before_any_click(self):
        for category in CATEGORIES:
            self.save(category, content=f"Precomputed evidence for {category}")

        response = self.client.get("/research/AAA")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")
        for category in CATEGORIES:
            self.assertIn(f"Precomputed evidence for {category}", response.text)
        section_buttons = [button for button in soup.find_all("button")
                           if "open = open" in button.get("@click", "")]
        self.assertEqual(len(section_buttons), 9)
        self.assertTrue(all(not button.has_attr("hx-get") for button in section_buttons))
        self.assertFalse(soup.select('[hx-get*="/section/"]'))
        self.prepare.assert_not_called()

    def test_warm_section_does_not_wait_for_a_busy_model_generation_slot(self):
        self.save("financial_snapshot", content="Revenue already prepared")
        busy_slot = threading.Lock()
        busy_slot.acquire()
        self.addCleanup(busy_slot.release)

        with patch.object(cache, "_slot", busy_slot):
            response = self.client.get("/research/AAA/section/financial_snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Revenue already prepared", response.text)
        self.assertFalse(self.section_polls(response, "financial_snapshot"))
        self.prepare.assert_not_called()

    def test_missing_company_sections_queue_one_company_job_without_blocking_saved_sections(self):
        for category in CATEGORIES:
            if category != "guidance":
                self.save(category)
        self.prepare.return_value = RUNNING_JOB

        response = self.client.get("/research/AAA")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Saved business_snapshot answer", response.text)
        self.assertIn("Saved quarterly comparison", response.text)
        self.prepare.assert_called_once_with("AAA")
        soup = BeautifulSoup(response.text, "html.parser")
        self.assertIsNotNone(soup.find("button", attrs={"hx-get": "/research/AAA/section/guidance"}))

    def test_stale_section_keeps_its_old_answer_visible_while_preparing_new_evidence(self):
        self.save("business_snapshot", content="Previous saved company overview", revision="old-evidence")
        self.prepare.return_value = RUNNING_JOB

        response = self.client.get("/research/AAA/section/business_snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Previous saved company overview", response.text)
        self.assertIn("saved answer", response.text.lower())
        self.assertIn("background", response.text.lower())
        self.assertTrue(self.section_polls(response, "business_snapshot"))
        self.prepare.assert_called_once_with("AAA")

    def test_missing_section_returns_a_polling_placeholder(self):
        self.prepare.return_value = RUNNING_JOB

        response = self.client.get("/research/AAA/section/guidance")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.section_polls(response, "guidance"))
        self.prepare.assert_called_once_with("AAA")
        self.assertFalse(BeautifulSoup(response.text, "html.parser").select(".research-content"))

    def test_absent_source_evidence_does_not_create_an_endless_poll(self):
        (self.indexes / "AAA.json").unlink()
        self.assertFalse(pull.document_status("AAA")["ready"])

        response = self.client.get("/research/AAA/section/guidance")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.section_polls(response, "guidance"))
        self.assertTrue(BeautifulSoup(response.text, "html.parser").get_text(strip=True))

    def test_unknown_section_is_rejected_without_starting_preparation(self):
        response = self.client.get("/research/AAA/section/unsupported")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Unknown section", response.text)
        self.prepare.assert_not_called()

    def test_corrupt_saved_section_is_treated_as_missing_and_queued(self):
        self.save("business_snapshot", raw="{broken json")
        self.prepare.return_value = RUNNING_JOB

        response = self.client.get("/research/AAA/section/business_snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.section_polls(response, "business_snapshot"))
        self.prepare.assert_called_once_with("AAA")
        self.assertNotIn("broken json", response.text)

    def test_wrong_saved_payload_shape_cannot_crash_either_section_template(self):
        self.prepare.return_value = RUNNING_JOB
        for category, payload in (("business_snapshot", []), ("promised_vs_delivered", {"content": "Wrong shape"})):
            with self.subTest(category=category):
                self.save(category, raw=json.dumps(payload))

                response = self.client.get(f"/research/AAA/section/{category}")

                self.assertEqual(response.status_code, 200)
                self.assertTrue(self.section_polls(response, category))
                self.assertNotIn("Wrong shape", response.text)

    def test_document_status_does_not_load_the_vector_index(self):
        with patch.object(pull, "load_index", side_effect=AssertionError("Status must not parse large vector files")):
            documents = pull.document_status("AAA")

        self.assertTrue(documents["ready"])
        self.assertEqual(documents["labels"], [])


if __name__ == "__main__":
    unittest.main()
