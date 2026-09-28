import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db
from app.pipeline import pull_pipeline as pull
from app.presentation import render_markdown, source_url


DOCS = {
    "annual_reports": [{"year": 2026, "url": "https://example.com/annual-2026.pdf"}],
    "concalls": [{"label": "Jul 2026", "transcript_url": "https://example.com/call-jul.pdf"}],
}


class ResearchPresentationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db_patch = patch("app.db.DB_PATH", str(Path(folder.name) / "research.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        init_db()

    def test_markdown_formats_headings_lists_and_tables(self):
        result = str(render_markdown(
            "## Overview\n\n**At a glance**\n\n- Revenue grew\n- Margin improved\n\n"
            "| Metric | Value |\n| --- | --- |\n| Revenue | 100 |"
        ))
        for expected in ("<h2>Overview</h2>", "<strong>At a glance</strong>",
                         "<ul>", "<li>Revenue grew</li>", "<table>", "<td>100</td>"):
            self.assertIn(expected, result)
        self.assertNotIn("**At a glance**", result)

    def test_markdown_does_not_execute_html_or_unsafe_links(self):
        result = str(render_markdown(
            '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n'
            '[bad](javascript:alert(1))\n\n[good](https://example.com/report.pdf)'
        ))
        self.assertNotIn("<script>", result)
        self.assertNotIn("<img", result)
        self.assertNotIn('href="javascript:', result)
        self.assertIn('href="https://example.com/report.pdf"', result)

    def test_source_links_require_a_web_url(self):
        for value in (None, "", "javascript:alert(1)", "data:text/html,test", "//example.com", "https:///missing-host"):
            self.assertIsNone(source_url(value))
        self.assertEqual(source_url(DOCS["annual_reports"][0]["url"]), DOCS["annual_reports"][0]["url"])

    def test_existing_index_gets_urls_without_reembedding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "TEST.json"
            original = [{"label": "Annual Report 2026", "text": "Annual excerpt", "embedding": [1, 2]},
                        {"label": "Concall Jul 2026", "text": "Call excerpt", "embedding": [3, 4]}]
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch.object(pull, "INDEX_DIR", Path(folder)), \
                 patch.object(pull, "fetch_soup", return_value=object()) as fetch, \
                 patch.object(pull, "get_documents", return_value=DOCS), \
                 patch.object(pull, "embed") as embed, \
                 patch.object(pull, "fetch_and_extract") as download:
                updated = pull.build_index("TEST")
                self.assertEqual(updated[0]["url"], DOCS["annual_reports"][0]["url"])
                self.assertEqual(updated[1]["url"], DOCS["concalls"][0]["transcript_url"])
                self.assertEqual(updated[0]["embedding"], [1, 2])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), updated)
                embed.assert_not_called()
                download.assert_not_called()
                pull.build_index("TEST")
                fetch.assert_called_once()

    def test_new_index_retains_the_downloaded_document_url(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(pull, "INDEX_DIR", Path(folder)), \
             patch.object(pull, "fetch_soup", return_value=object()), \
             patch.object(pull, "get_documents", return_value=DOCS), \
             patch.object(pull, "fetch_and_extract", return_value="A short source excerpt."), \
             patch.object(pull, "embed", return_value=[[1, 0], [0, 1]]):
            chunks = pull.build_index("TEST")
            self.assertEqual([c["url"] for c in chunks],
                             [DOCS["annual_reports"][0]["url"], DOCS["concalls"][0]["transcript_url"]])

    def test_sources_come_from_retrieved_documents(self):
        chunks = [{"label": "Annual Report 2026", "text": "Excerpt", "url": DOCS["annual_reports"][0]["url"]}] * 2
        with patch.object(pull, "retrieve", return_value=chunks), \
             patch.object(pull, "chat_text", return_value="**At a glance**"):
            data = pull.generate_section("TEST", "business_snapshot")
        self.assertEqual(data["sources"], [{"label": "Annual Report 2026", "url": DOCS["annual_reports"][0]["url"]}])

    def test_research_response_has_formatted_content_and_clickable_sources(self):
        data = {"content": "**At a glance**\n\n- Revenue grew", "sources": [
            {"label": "Annual Report 2026", "url": DOCS["annual_reports"][0]["url"]},
            {"label": "Concall Jul 2026", "url": DOCS["concalls"][0]["transcript_url"]},
            {"label": "Unavailable source", "url": None},
            {"label": "Unsafe source", "url": "javascript:alert(1)"},
        ]}
        with patch("app.main.generate_section", return_value=data):
            response = TestClient(app).get("/research/TEST/section/business_snapshot")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, "html.parser")
        self.assertEqual(soup.strong.text, "At a glance")
        links = soup.find_all("a")
        self.assertEqual([a["href"] for a in links],
                         [DOCS["annual_reports"][0]["url"], DOCS["concalls"][0]["transcript_url"]])
        self.assertTrue(all(a["target"] == "_blank" and "noopener" in a["rel"] for a in links))

    def test_guidance_source_links_to_the_transcript(self):
        quarter = {"quarter": "Jun 2026", "verdict": "unclear", "rationale": "**Insufficient guidance**",
                   "actual": {m: {"qoq_pct": None} for m in ("revenue", "ebitda", "pbt", "pat")},
                   "guidance_source": "Apr 2026", "guidance_source_url": "https://example.com/call-apr.pdf"}
        with patch("app.main.build_promised_vs_delivered", return_value=[quarter]):
            response = TestClient(app).get("/research/TEST/section/promised_vs_delivered")
        soup = BeautifulSoup(response.text, "html.parser")
        self.assertEqual(soup.strong.text, "Insufficient guidance")
        self.assertEqual(soup.a["href"], quarter["guidance_source_url"])


if __name__ == "__main__":
    unittest.main()
