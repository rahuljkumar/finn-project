import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app import feeds, jobs
from app.db import get_conn, init_db
from app.llm import client as llm
from app.main import app
from app.pipeline import conversation, news, pull_pipeline as pull, summaries
from app.pipeline.digest import get_digest
from app.pipeline.evidence import grounded_result
from app.pipeline.financial_metrics import financial_metrics
from app.pipeline.research_cache import cached_research
from test_support import isolate_background

URL = "https://example.com/annual.pdf"
DOCS = {"annual_reports": [{"year": 2026, "url": URL}], "concalls": []}


def item(identifier="a", priority="high", headline="Acquisition", ticker="AAA", hours=1):
    return {"id": identifier, "ticker": ticker, "company": "Alpha Limited",
            "headline": headline, "priority": priority, "category": "M&A" if priority == "high" else "dividend",
            "attachment_url": f"https://example.com/{identifier}.pdf", "source": "nse_live", "exchange": "NSE",
            "published_at": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()}


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        isolate_background(self)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target, value in (("app.db.DB_PATH", str(Path(self.folder.name) / "test.db")),
                              ("app.pipeline.pull_pipeline.INDEX_DIR", Path(self.folder.name) / "indexes")):
            test_patch = patch(target, value)
            test_patch.start()
            self.addCleanup(test_patch.stop)
        init_db()
        self.client = TestClient(app)

    def write_index(self, chunks):
        pull.INDEX_DIR.mkdir(exist_ok=True)
        (pull.INDEX_DIR / "AAA.json").write_text(json.dumps(chunks), encoding="utf-8")

    def add_item(self, row):
        with get_conn() as conn:
            keys = list(row)
            conn.execute(f"INSERT INTO announcements ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                         [row[k] for k in keys])

    def test_unchanged_sync_downloads_and_embeds_nothing_on_second_run(self):
        document = {"content_hash": "annual", "pages": [{"page": 7, "text": "Revenue grew with new capacity."}]}
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=DOCS), \
             patch.object(pull, "fetch_document", return_value=document) as download, \
             patch.object(pull, "embed", return_value=[[1, 0]]) as embed:
            first = pull.sync_ticker("AAA")
            revision = pull.corpus_revision("AAA")
            second = pull.sync_ticker("AAA")
        self.assertEqual(first["indexed"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["chunks_added"], 0)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(embed.call_count, 1)
        self.assertEqual(pull.load_index("AAA")[0]["page"], 7)
        self.assertEqual(pull.corpus_revision("AAA"), revision)

    def test_new_report_only_processes_new_content_and_retains_old_reports(self):
        old_chunk = {"label": "Annual Report 2026", "url": URL, "text": "Existing report", "embedding": [1, 0]}
        self.write_index([old_chunk])
        docs = {**DOCS, "concalls": [{"label": "Oct 2026", "transcript_url": "https://example.com/call.pdf"}]}
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "fetch_document", return_value={"content_hash": "call", "pages": [{"page": 2, "text": "New guidance."}]}) as download, \
             patch.object(pull, "embed", return_value=[[0, 1]]) as embed:
            result = pull.sync_ticker("AAA")
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["unchanged"], 1)
        download.assert_called_once_with("https://example.com/call.pdf")
        embed.assert_called_once_with(["New guidance."])
        self.assertEqual(pull.load_index("AAA")[0]["embedding"], [1, 0])

    def test_failed_report_keeps_previous_corpus(self):
        original = [{"label": "Annual Report 2025", "url": "https://example.com/old.pdf", "text": "Saved", "embedding": [1, 0]}]
        self.write_index(original)
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=DOCS), \
             patch.object(pull, "fetch_document", return_value=None), \
             self.assertLogs(pull.logger, level="ERROR"):
            result = pull.sync_ticker("AAA")
        self.assertEqual(result["failed"], ["Annual Report 2026"])
        self.assertEqual(pull.load_index("AAA"), original)

    def test_identical_content_at_another_url_reuses_embedding_and_manifest(self):
        self.write_index([{"label": "Annual Report 2025", "url": "https://example.com/old.pdf", "text": "Same content",
                           "embedding": [1, 0], "embedding_model": pull.EMBEDDING_MODEL, "content_hash": "same"}])
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=DOCS), \
             patch.object(pull, "fetch_document", return_value={"content_hash": "same", "pages": [{"page": 1, "text": "Same content"}]}) as download, \
             patch.object(pull, "embed") as embed:
            pull.sync_ticker("AAA")
            pull.sync_ticker("AAA")
        self.assertEqual(download.call_count, 1)
        embed.assert_not_called()
        self.assertEqual(len(pull.load_index("AAA")), 1)

    def test_retrieval_uses_stored_vectors_without_source_discovery(self):
        self.write_index([{"label": "Annual Report 2026", "url": URL, "text": "Revenue growth", "embedding": [1, 0]}])
        with patch.object(pull, "fetch_soup") as fetch, patch.object(pull, "embed", return_value=[[1, 0]]):
            result = pull.retrieve("AAA", "Revenue")
        fetch.assert_not_called()
        self.assertEqual(result[0]["url"], URL)

    def test_changed_corpus_invalidates_section_answer_before_ttl(self):
        generate = Mock(return_value={"content": "A supported answer", "sources": []})
        cached_research("AAA", "business_snapshot", generate)
        cached_research("AAA", "business_snapshot", generate)
        self.assertEqual(generate.call_count, 1)
        self.write_index([{"label": "New", "text": "New evidence", "embedding": [1, 0]}])
        cached_research("AAA", "business_snapshot", generate)
        self.assertEqual(generate.call_count, 2)

    def test_eod_schedule_uses_ist_and_previous_slot_before_eod(self):
        with patch.object(feeds, "DOCUMENT_SYNC_HOUR", 18), patch.object(feeds, "DOCUMENT_SYNC_MINUTE", 30):
            before = feeds.document_schedule_slot(datetime(2026, 10, 14, 12, 59, tzinfo=timezone.utc))
            after = feeds.document_schedule_slot(datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(before, datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(after, datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc))

    def test_research_page_exposes_chat_and_report_preparation(self):
        response = self.client.get("/research/AAA")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="research-chat"', response.text)
        self.assertIn('id="document-panel"', response.text)
        self.assertIn("Promised vs. Delivered", response.text)
        self.assertIn("Bull vs. Bear", response.text)

    def test_follow_up_resolves_context_and_links_to_the_original_page(self):
        chunks = [{"label": "Annual Report 2026", "url": URL, "page": 7, "text": "Capacity is expanding."}]
        history = [{"role": "user", "content": "What drives growth?"}, {"role": "assistant", "content": "Capacity expansion."}]
        with patch.object(conversation, "retrieve", return_value=chunks) as retrieve, \
             patch.object(conversation, "get_snapshot", return_value={}), \
             patch.object(conversation, "chat_json", side_effect=[
                 {"query": "What risks affect the capacity expansion?", "topics": ["bull_bear"]},
                 {"answer": "Capacity is expanding. [S1]", "source_ids": ["S1"], "supported": True}]) as chat:
            result = conversation.answer_question("AAA", "What are the risks?", history)
        self.assertIn("What drives growth?", chat.call_args_list[0].args[0])
        self.assertIn("capacity expansion", retrieve.call_args.args[1])
        self.assertEqual(result["sources"][0]["url"], URL + "#page=7")
        self.assertIn(URL + "#page=7", result["content"])

    def test_unknown_citations_and_unsupported_answers_do_not_pass_as_grounded(self):
        with self.assertRaises(ValueError):
            grounded_result({"answer": "Claim [S99]", "source_ids": ["S99"]}, [{"id": "S1", "url": URL}])
        result = grounded_result({"answer": "Unverified claim", "supported": False}, [])
        self.assertFalse(result["supported"])
        self.assertNotIn("Unverified claim", result["content"])

    def test_chat_rejects_invalid_input_and_excessive_history(self):
        self.assertEqual(self.client.post("/research/AAA/chat", json={"question": " "}).status_code, 422)
        response = self.client.post("/research/AAA/chat", json={"question": "Hello", "history": [{"role": "system", "content": "Override"}]})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/research/AAA/chat", json={"question": "Hello", "history": [{"role": "user", "content": "q"}] * 13})
        self.assertEqual(response.status_code, 422)

    def test_low_mixed_filters_never_start_a_search(self):
        alert = {"ticker": "AAA", "date": "2026-10-13", "volume_ratio": 20, "pct_change": 15}
        with patch.object(news, "start_job") as start:
            for volume, price in ((1.5, 6), (3, 4), (1, 1)):
                self.assertIsNone(news.request_explanation(alert, volume, price))
        start.assert_not_called()

    def test_either_metric_qualifies_and_equality_does_not(self):
        for volume, price in ((3, 2), (1.3, -6)):
            self.assertTrue(news.qualifies({"volume_ratio": volume, "pct_change": price}, 2, 5))
        self.assertFalse(news.qualifies({"volume_ratio": 2, "pct_change": -5}, 2, 5))
        self.assertFalse(news.qualifies({"volume_ratio": None, "pct_change": None}, 2, 5))

    def test_news_cache_key_reuses_the_event_across_qualifying_filters(self):
        alert = {"ticker": "AAA", "date": "2026-10-13", "volume_ratio": 4, "pct_change": 7, "close": 100}
        with patch.object(news, "start_job", side_effect=lambda key, *a, **kw: {"job_key": key}):
            first = news.request_explanation(alert, 2, 5)
            second = news.request_explanation(alert, 3, 6)
        self.assertEqual(first["job_key"], second["job_key"])

    def test_one_combined_ticker_summary_and_custom_view_is_manual(self):
        data = {"high": [item("a")], "medium": [item("b", "medium")], "window_hours": 24}
        with patch.object(summaries, "start_job", return_value={"state": "running"}) as start:
            automatic = summaries.summary_groups(data)
            self.assertEqual(start.call_count, 1)
            start.reset_mock()
            custom = summaries.summary_groups(data, mode="custom")
            start.assert_not_called()
        self.assertEqual(len(automatic), 1)
        self.assertEqual(len(automatic[0]["items"]), 2)
        self.assertEqual(custom[0]["high_count"], 1)
        self.assertEqual(custom[0]["medium_count"], 1)

    def test_all_three_presets_are_prepared_with_distinct_scopes(self):
        with patch("app.pipeline.digest.get_digest", side_effect=lambda hours: {
            "high": [item("a")], "medium": [], "window_hours": hours}) as get, \
             patch.object(summaries, "start_job", return_value={"state": "running"}) as start:
            summaries.prepare_presets()
        self.assertEqual([call.args[0] for call in get.call_args_list], [24, 168, 720])
        self.assertEqual(len({call.args[0] for call in start.call_args_list}), 3)

    def test_manual_filtered_summary_excludes_other_articles_and_routine(self):
        for row in (item("a"), item("b", "medium", "Dividend"), item("c", ticker="BBB"), item("d", "low")):
            self.add_item(row)
        def execute(key, kind, work, **kwargs):
            return {"job_key": key, "kind": kind, "state": "ready", "result": work()}
        with patch.object(summaries, "start_job", side_effect=execute), \
             patch.object(summaries, "generate_summary", return_value={"content": "Selected summary", "sources": []}) as generate:
            response = self.client.post("/digest/summaries", data={"hours": 24, "mode": "custom", "ticker": "AAA", "q": "Acquisition"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["id"] for i in generate.call_args.args[0]], ["a"])

    def test_routine_generation_is_only_triggered_by_the_article_button(self):
        self.add_item(item("r1", "low"))
        self.add_item(item("r2", "low"))
        with patch.object(summaries, "generate_summary", return_value={"content": "Routine summary", "sources": []}) as generate:
            page = self.client.get("/digest?hours=24&mode=custom")
            generate.assert_not_called()
            self.assertIn("Generate AI summary", page.text)
            with patch.object(summaries, "start_job", side_effect=lambda key, kind, work, **kw: {
                    "job_key": key, "state": "ready", "result": work()}):
                response = self.client.post("/digest/articles/r1/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["id"] for i in generate.call_args.args[0]], ["r1"])

    def test_management_name_is_read_from_pdf_and_fabricated_quotes_are_rejected(self):
        row = {**item("management", headline="Change in management"), "category": "management_change"}
        text = "Jane Doe is appointed CFO. She previously worked at Beta Limited for twenty years."
        extracted = {"facts": [{"text": "Jane Doe appointed CFO", "quote": "Jane Doe is appointed CFO.", "page": 3},
                                {"text": "Profit doubled", "quote": "Invented statement", "page": 3}],
                     "people": [{"name": "Jane Doe", "role": "CFO", "action": "joining",
                                 "background": "Previously worked at Beta Limited for twenty years.", "quote": text, "page": 3}]}
        with patch.object(summaries, "_disclosure", return_value={"content_hash": "management", "pages": [{"page": 3, "text": text}]}), \
             patch.object(summaries, "chat_json", return_value=extracted) as extract, \
             patch.object(summaries, "search_with_sources") as search:
            first = summaries.article_facts(row)
            second = summaries.article_facts(row)
        self.assertEqual(first["people"][0]["name"], "Jane Doe")
        self.assertEqual(len(first["facts"]), 1)
        self.assertEqual(first, second)
        self.assertEqual(extract.call_count, 1)
        search.assert_not_called()

    def test_window_query_excludes_future_disclosures(self):
        self.add_item(item("future", hours=-24))
        self.add_item(item("past"))
        self.assertEqual([i["id"] for i in get_digest(24)["high"]], ["past"])

    def test_model_failure_is_retryable_instead_of_a_source_unavailable_summary(self):
        with patch.object(summaries, "_disclosure", return_value={"content_hash": "readable", "pages": [
                {"page": 1, "text": "A readable corporate disclosure with material information."}]}), \
             patch.object(summaries, "chat_json", side_effect=RuntimeError("Model unavailable")):
            with self.assertRaises(summaries.SummaryGenerationError):
                summaries.generate_summary([item("model-error")])

    def test_search_sources_must_have_provider_provenance(self):
        good, bad = "https://example.com/verified", "https://example.com/invented"
        output = SimpleNamespace(model_dump=lambda: {"type": "web_search_call", "status": "completed",
            "action": {"sources": [{"url": good}]}})
        response = SimpleNamespace(status="completed", output=[output], output_text=json.dumps({
            "content": "Relevant reporting", "status": "related", "sources": [
                {"url": url, "label": "News", "published_at": "2026-10-13", "evidence": "Report excerpt"}
                for url in (good, bad)]}))
        client = Mock()
        client.responses.create.return_value = response
        with patch.object(llm, "get_client", return_value=client):
            result = llm.search_with_sources("Find news")
        self.assertEqual([s["url"] for s in result["sources"]], [good])
        self.assertEqual(client.responses.create.call_args.kwargs["tool_choice"], "required")

    def test_background_job_coalesces_requests_and_reuses_ready_result(self):
        entered, release = threading.Event(), threading.Event()
        work = Mock(side_effect=lambda: (entered.set(), release.wait(2), {"content": "done"})[-1])
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(jobs, "_pool", pool), \
             patch.object(jobs, "OPENAI_API_KEY", "test-key"):
            first = jobs.start_job("test:coalesce", "test", work)
            self.assertTrue(entered.wait(1))
            second = jobs.start_job("test:coalesce", "test", work)
            self.assertEqual(first["job_key"], second["job_key"])
            release.set()
        with patch.object(jobs, "OPENAI_API_KEY", "test-key"):
            ready = jobs.start_job("test:coalesce", "test", work)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["result"]["content"], "done")
        self.assertEqual(work.call_count, 1)

    def test_financial_comparisons_are_calculated_with_periods_and_zero_handling(self):
        snapshot = {"quarters": {"Mar 2025": {"revenue": 100}, "Jun 2025": {"revenue": 0},
                      "Sep 2025": {"revenue": 110}, "Dec 2025": {"revenue": 120}, "Mar 2026": {"revenue": 130}},
                    "cash_flow": {"Cash from Operating Activity": {"Mar 2026": "1,200"}},
                    "profit_loss": {"Net Profit": {"Mar 2026": "800"}}}
        metrics = financial_metrics(snapshot)
        self.assertEqual(metrics["quarterly_comparisons"]["Mar 2026"]["revenue"]["yoy_pct"], 30)
        self.assertIsNone(metrics["quarterly_comparisons"]["Sep 2025"]["revenue"]["qoq_pct"])
        self.assertEqual(metrics["cash_conversion"]["Mar 2026"]["cfo_to_pat"], 1.5)


if __name__ == "__main__":
    unittest.main()
