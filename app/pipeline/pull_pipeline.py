"""Pull mode: primary-source-only research over a company's own annual
reports and concall transcripts. No news, no broker notes, no model
"prior knowledge" of the company -- every claim must be grounded in a
retrieved excerpt or the structured screener.in numbers, both sourced
from the company's own filings.
"""

import json
import logging
from pathlib import Path

import numpy as np

from app.config import BASE_DIR
from app.llm.client import chat_text, embed
from app.pipeline.documents import chunk_text, fetch_and_extract
from app.sources.screener_client import get_documents, fetch_soup

logger = logging.getLogger(__name__)

INDEX_DIR = BASE_DIR / "data" / "rag_index"
# promised_vs_delivered needs, for each of the last 4 *reported* quarters,
# the concall that set guidance for it -- for the oldest of those 4
# quarters that can reach back ~7-8 transcripts in recency order, so this
# must cover well more than "last 4" or those lookups silently come back
# empty. Concalls are cheap to embed (~50-60 chunks each) so the extra
# headroom costs little.
MAX_CONCALLS = 8
MAX_ANNUAL_REPORTS = 1  # most recent year is enough for snapshot/balance-sheet color; keeps embedding volume down

SYSTEM_PROMPT = (
    "You are a financial research analyst producing output for an investor. "
    "Ground every claim ONLY in the provided source excerpts, which come from "
    "the company's own annual report and earnings-call transcripts (or "
    "structured figures taken directly from those same filings). Never use "
    "outside knowledge about this company, and never rely on news, broker "
    "notes, or research reports. If the excerpts don't contain enough "
    "information for a claim, say so explicitly instead of guessing. Cite the "
    "source label (e.g. 'Concall Jul 2026' or 'Annual Report 2026') for every "
    "factual claim. Be concise -- this is a screen on a phone, not a report."
)

CATEGORY_QUERIES = {
    "business_snapshot": "business description segments products services revenue mix operations overview",
    "financial_snapshot": "revenue margins profit key financial ratios highlights this year",
    "trajectory": "multi-year growth trend revenue profit margin history direction",
    "balance_sheet": "balance sheet debt borrowings leverage working capital funding structure",
    "cash_quality": "cash flow from operations free cash flow cash conversion profit to cash",
    "narrative_vs_numbers": "management discussion and analysis outlook commentary compared to reported financial results",
    "guidance": "management guidance outlook growth targets margin capex plans next year commentary",
    "bull_bear": "risks opportunities strengths challenges competitive position concerns",
}

CATEGORY_PROMPTS = {
    "business_snapshot": "Describe what the company does: its segments and revenue mix. 3-5 sentences.",
    "financial_snapshot": "Give the current headline numbers at a glance: revenue, margins, PAT, and 2-3 key ratios. Use the structured data provided; use the excerpts for color.",
    "trajectory": "Describe the multi-year direction using the structured annual data provided: what's growing, what's compressing. Call out the specific trend (e.g. margin expansion/contraction over the last N years).",
    "balance_sheet": "Assess debt/leverage, working-capital cycle, and funding structure using the structured data provided.",
    "cash_quality": "Assess the cash-flow trend and cash conversion: do profits turn into cash? Use the structured cash-flow data and CFO/OP ratio provided.",
    "narrative_vs_numbers": "Compare what management says in the concall/MD&A commentary against what the actual financials show. Where do they agree, and where do they diverge?",
    "guidance": "State what management has explicitly guided on -- growth, margins, capex -- in their own words from the transcripts. If no explicit guidance was found in the excerpts, say so.",
    "bull_bear": "Give a tight bull case and a tight bear case, each 2-3 sentences, grounded in the excerpts.",
}

DEEP_CATEGORIES = {"financial_snapshot", "trajectory"}


def _index_path(ticker: str) -> Path:
    return INDEX_DIR / f"{ticker}.json"


def build_index(ticker: str, force: bool = False) -> list[dict]:
    path = _index_path(ticker)
    if path.exists() and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    soup = fetch_soup(ticker)
    if soup is None:
        return []
    docs = get_documents(soup)

    sources = []
    for ar in docs["annual_reports"][:MAX_ANNUAL_REPORTS]:
        sources.append({"doc_type": "annual_report", "label": f"Annual Report {ar['year']}", "url": ar["url"]})
    for cc in docs["concalls"][:MAX_CONCALLS]:
        if cc.get("transcript_url"):
            sources.append({"doc_type": "concall", "label": f"Concall {cc['label']}", "url": cc["transcript_url"]})

    chunks = []
    for src in sources:
        text = fetch_and_extract(src["url"])
        if not text:
            continue
        for i, chunk in enumerate(chunk_text(text)):
            chunks.append({"doc_type": src["doc_type"], "label": src["label"], "chunk_index": i, "text": chunk})

    if not chunks:
        return []

    embeddings = embed([c["text"] for c in chunks])
    for c, e in zip(chunks, embeddings):
        c["embedding"] = e

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(chunks, f)
    return chunks


def retrieve(ticker: str, query: str, k: int = 8) -> list[dict]:
    chunks = build_index(ticker)
    if not chunks:
        return []
    q_emb = np.array(embed([query])[0])
    mat = np.array([c["embedding"] for c in chunks])
    norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(q_emb) + 1e-8
    sims = (mat @ q_emb) / norms
    top_idx = np.argsort(-sims)[:k]
    return [chunks[i] for i in top_idx]


def generate_section(ticker: str, category: str, structured: dict | None = None) -> dict:
    chunks = retrieve(ticker, CATEGORY_QUERIES[category])
    if not chunks and not structured:
        return {
            "content": "No primary-source documents (annual report / concall transcript) available for this company yet.",
            "sources": [],
        }

    context = "\n\n".join(f"[{c['label']}]\n{c['text']}" for c in chunks)
    structured_block = (
        f"\n\nStructured data (from the company's own filings, via screener.in):\n{json.dumps(structured, indent=2)}"
        if structured
        else ""
    )
    prompt = f"{CATEGORY_PROMPTS[category]}\n\nSource excerpts:\n{context}{structured_block}"

    try:
        content = chat_text(prompt, tier="reasoning", system=SYSTEM_PROMPT)
    except Exception as e:
        logger.warning("Pull-mode generation failed for %s/%s: %s", ticker, category, e)
        content = "Couldn't generate this section right now."

    sources = sorted({c["label"] for c in chunks})
    return {"content": content, "sources": sources}
