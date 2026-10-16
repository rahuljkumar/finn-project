"""Pull mode: primary-source-only research over a company's own annual
reports and concall transcripts. No news, no broker notes, no model
"prior knowledge" of the company -- every claim must be grounded in a
retrieved excerpt or the structured screener.in numbers, both sourced
from the company's own filings.
"""

import json
import logging
import hashlib
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from app.config import DATA_DIR, EMBEDDING_MODEL, load_portfolio
from app.db import get_conn
from app.llm.client import chat_text, embed
from app.pipeline.documents import document_chunks, download_pdf, fetch_document
from app.sources.screener_client import get_documents, fetch_soup, get_annual_table, get_quarterly_table
from app.storage import atomic_write

logger = logging.getLogger(__name__)

INDEX_DIR = DATA_DIR / "rag_index"
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


def _document_sources(docs: dict) -> list[dict]:
    sources = [
        {"doc_type": "annual_report", "label": f"Annual Report {ar['year']}", "url": ar["url"]}
        for ar in docs["annual_reports"]
    ]
    sources.extend(
        {"doc_type": "concall", "label": f"Concall {cc['label']}", "url": cc["transcript_url"]}
        for cc in docs["concalls"]
        if cc.get("transcript_url")
    )
    return sources


def _report_in_window(source: dict, since: date, as_of: date) -> bool | None:
    if source["doc_type"] == "annual_report":
        year = int(source["label"].rsplit(" ", 1)[1])
        return since.year < year <= as_of.year
    label = source["label"].removeprefix("Concall ").strip()
    for pattern in ("%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y"):
        try:
            # Month-only labels use their first day; never assume a later
            # publication date to bring a report inside the selected window.
            period = datetime.strptime(label, pattern).date()
            return since <= period <= as_of
        except ValueError:
            continue
    return None


def load_index(ticker: str) -> list[dict]:
    path = _index_path(ticker)
    if not path.exists():
        return []
    try:
        chunks = json.loads(path.read_text(encoding="utf-8"))
        return chunks if isinstance(chunks, list) else []
    except (OSError, ValueError):
        logger.warning("Unreadable stored index for %s; run document sync to repair it", ticker)
        return []


def get_snapshot(ticker: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT payload FROM research_snapshots WHERE ticker=?", (ticker,)).fetchone()
    return json.loads(row[0]) if row else {}


def corpus_revision(ticker: str) -> str:
    path = _index_path(ticker)
    with get_conn() as conn:
        row = conn.execute("SELECT revision FROM research_snapshots WHERE ticker=?", (ticker,)).fetchone()
        documents = [tuple(r) for r in conn.execute("""SELECT url,label,doc_type,content_hash
            FROM research_documents WHERE ticker=? AND content_hash IS NOT NULL ORDER BY url""", (ticker,))]
    if not path.exists():
        evidence = "empty"
    elif documents:
        # Checked-at timestamps, download errors on retained reports, and
        # touching an unchanged index do not change the available evidence.
        evidence = documents
    else:
        # Earlier indexes acquire a content manifest at their next sync.
        # Until then, detect changes without parsing a large vector file.
        stamp = path.stat()
        evidence = [stamp.st_mtime_ns, stamp.st_size]
    signature = [EMBEDDING_MODEL, evidence, row[0] if row else ""]
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]


def _has_index(ticker: str) -> bool:
    """Check for saved chunks without loading their potentially huge vectors."""
    try:
        with _index_path(ticker).open("rb") as stream:
            header = stream.read(128).lstrip()
        return header.startswith(b"[") and header[1:].lstrip().startswith(b"{")
    except OSError:
        return False


def document_status(ticker: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT label,status,checked_at FROM research_documents WHERE ticker=?", (ticker,)).fetchall()
        snapshot = conn.execute("SELECT saved_at FROM research_snapshots WHERE ticker=?", (ticker,)).fetchone()
    return {"ready": _has_index(ticker), "labels": sorted({r["label"] for r in rows if r["status"] == "indexed"}),
            "failed": sum(r["status"] == "error" for r in rows),
            "checked_at": snapshot[0] if snapshot else None}


def _record_document(ticker, source, content_hash, status):
    with get_conn() as conn:
        conn.execute("""INSERT INTO research_documents VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(ticker,url) DO UPDATE SET label=excluded.label,doc_type=excluded.doc_type,
            content_hash=excluded.content_hash,status=excluded.status,checked_at=excluded.checked_at""",
            (ticker, source["url"], source["label"], source["doc_type"], content_hash, status,
             datetime.now(timezone.utc).isoformat()))


_sync_guard = threading.Lock()
_sync_locks = {}


def sync_ticker(ticker: str, force: bool = False, *, all_reports: bool = False,
                progress: Callable[[dict], None] | None = None,
                since: date | None = None, as_of: date | None = None,
                prepare_sections: bool = False) -> dict:
    """Sync recent reports, or the entire discovered archive for a backfill.

    Full backfills also ensure the PDFs behind reused vectors are on disk.
    Optional dates restrict downloads to the selected reporting window.
    A progress callback receives one terminal event per report.
    The web service enables prepare_sections; standalone ingestion commands
    leave AI section preparation to their explicit follow-up command.
    """
    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", ticker):
        raise ValueError("Invalid ticker")
    with _sync_guard:
        lock = _sync_locks.setdefault(ticker, threading.Lock())
    with lock:
        result = _sync_ticker(ticker, force, all_reports=all_reports, progress=progress, since=since, as_of=as_of)
    if prepare_sections and ticker in {c["ticker"] for c in load_portfolio()["portfolio"]}:
        try:
            from app.pipeline.prepare_research import request_research_preparation
            request_research_preparation(ticker)
        except Exception:
            logger.exception("Could not queue saved Research sections for %s", ticker)
    return result


def _sync_ticker(ticker: str, force: bool, *, all_reports: bool = False,
                 progress: Callable[[dict], None] | None = None,
                 since: date | None = None, as_of: date | None = None) -> dict:
    soup = fetch_soup(ticker)
    if soup is None:
        raise RuntimeError("Screener company page is unavailable")
    docs = get_documents(soup)
    snapshot = {"profit_loss": get_annual_table(soup, "profit-loss"),
                "balance_sheet": get_annual_table(soup, "balance-sheet"),
                "cash_flow": get_annual_table(soup, "cash-flow"),
                "ratios": get_annual_table(soup, "ratios"),
                "quarters": get_quarterly_table(soup), "documents": docs,
                "source_url": f"https://www.screener.in/company/{ticker}/consolidated/"}
    payload = json.dumps(snapshot, sort_keys=True)
    with get_conn() as conn:
        conn.execute("""INSERT INTO research_snapshots VALUES (?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,saved_at=excluded.saved_at""",
            (ticker, payload, hashlib.sha256(payload.encode()).hexdigest(), datetime.now(timezone.utc).isoformat()))
        known = {r["url"]: dict(r) for r in conn.execute(
            "SELECT * FROM research_documents WHERE ticker=?", (ticker,)).fetchall()}
    available = _document_sources(docs)
    eligible, undated, skipped = available, [], 0
    if since:
        eligible = []
        end = as_of or datetime.now(timezone.utc).date()
        for source in available:
            in_window = _report_in_window(source, since, end)
            if in_window is None:
                undated.append(source)
            elif in_window:
                eligible.append(source)
            else:
                skipped += 1
    selected = eligible if all_reports else (
        [s for s in eligible if s["doc_type"] == "annual_report"][:MAX_ANNUAL_REPORTS]
        + [s for s in eligible if s["doc_type"] == "concall"][:MAX_CONCALLS])
    unique_sources = {}
    for source in selected:
        unique_sources.setdefault(source["url"], source)
    wanted = list(unique_sources.values())
    chunks = load_index(ticker)
    # Import the earlier index without paying for its embeddings again.
    by_label = {s["label"]: s for s in available}
    changed = False
    for chunk in chunks:
        source = by_label.get(chunk.get("label"))
        if source and not chunk.get("url"):
            chunk.update(url=source["url"], doc_type=source["doc_type"])
            changed = True
    summary = {"ticker": ticker, "checked": len(wanted), "new": 0, "indexed": 0,
               "unchanged": 0, "chunks_added": 0, "skipped_outside_window": skipped,
               "undated": undated, "failed": [f"Date unavailable: {s['label']}" for s in undated]}
    for position, source in enumerate(wanted, start=1):
        event = {"ticker": ticker, **source, "position": position, "total": len(wanted)}
        current = [c for c in chunks if c.get("url") == source["url"]]
        record = known.get(source["url"], {})
        compatible = current and all(c.get("embedding_model", EMBEDDING_MODEL) == EMBEDDING_MODEL for c in current)
        alias = record.get("status") == "indexed" and any(
            c.get("content_hash") and c["content_hash"] == record.get("content_hash")
            and c.get("embedding_model", EMBEDDING_MODEL) == EMBEDDING_MODEL for c in chunks)
        if not record and not current:
            summary["new"] += 1
        try:
            if not force and (compatible or alias):
                if all_reports and download_pdf(source["url"]) is None:
                    raise ValueError("Previously indexed PDF could not be downloaded")
                summary["unchanged"] += 1
                _record_document(ticker, source, record.get("content_hash") or (current[0].get("content_hash") if current else None), "indexed")
                event.update(status="unchanged", chunks_added=0)
            else:
                document = fetch_document(source["url"])
                if not document:
                    raise ValueError("No readable PDF text")
                duplicate = [c for c in chunks if c.get("content_hash") == document["content_hash"]
                             and c.get("embedding_model") == EMBEDDING_MODEL]
                if duplicate and not force:
                    summary["unchanged"] += 1
                    _record_document(ticker, source, document["content_hash"], "indexed")
                    event.update(status="unchanged", chunks_added=0)
                else:
                    added = [{**source, **c, "content_hash": document["content_hash"],
                              "embedding_model": EMBEDDING_MODEL} for c in document_chunks(document)]
                    if not added:
                        raise ValueError("No chunks extracted")
                    vectors = embed([c["text"] for c in added])
                    for chunk, vector in zip(added, vectors, strict=True):
                        chunk["embedding"] = vector
                    updated = [c for c in chunks if c.get("url") != source["url"]] + added
                    atomic_write(_index_path(ticker), json.dumps(updated).encode("utf-8"))
                    chunks = updated
                    changed = False
                    _record_document(ticker, source, document["content_hash"], "indexed")
                    summary["indexed"] += 1
                    summary["chunks_added"] += len(added)
                    event.update(status="indexed", chunks_added=len(added))
        except Exception:
            logger.exception("Could not index %s / %s", ticker, source["label"])
            _record_document(ticker, source, record.get("content_hash"), "error")
            summary["failed"].append(source["label"])
            event.update(status="failed", chunks_added=0, error="Could not download, read, or embed this report.")
        if progress:
            progress(event)
    if changed:
        atomic_write(_index_path(ticker), json.dumps(chunks).encode("utf-8"))
    return summary


def sync_portfolio(tickers: list[str] | None = None, *, prepare_sections: bool = False) -> dict:
    if tickers is None:
        portfolio = load_portfolio()
        tickers = [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]
    results, failed = [], []
    for ticker in dict.fromkeys(tickers):
        try:
            result = sync_ticker(ticker, prepare_sections=prepare_sections)
            results.append(result)
            if result["failed"]:
                failed.append(ticker)
        except Exception:
            logger.exception("Document sync failed for %s", ticker)
            failed.append(ticker)
    return {"total": len(set(tickers)), "checked": sum(r["checked"] for r in results),
            "new": sum(r["new"] for r in results), "indexed": sum(r["indexed"] for r in results),
            "unchanged": sum(r["unchanged"] for r in results), "failed": failed}


def build_index(ticker: str, force: bool = False) -> list[dict]:
    """Explicit ingestion entry point; retrieval never calls this."""
    sync_ticker(ticker, force)
    return load_index(ticker)


def retrieve(ticker: str, query: str, k: int = 8) -> list[dict]:
    chunks = load_index(ticker)
    if not chunks:
        return []
    q_emb = np.array(embed([query])[0])
    mat = np.array([c["embedding"] for c in chunks])
    norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(q_emb) + 1e-8
    sims = (mat @ q_emb) / norms
    # Exact words/names complement semantic retrieval; diversify the sources.
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    lexical = np.array([len(words & set(re.findall(r"[a-z0-9]+", c["text"].lower()))) / max(len(words), 1) for c in chunks])
    order = np.argsort(-(sims + 0.15 * lexical))
    top_idx, counts = [], {}
    for i in order:
        label = chunks[i]["label"]
        if counts.get(label, 0) < 3:
            top_idx.append(i)
            counts[label] = counts.get(label, 0) + 1
        if len(top_idx) == k:
            break
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
        raise

    source_urls = {c["label"]: c.get("url") for c in chunks}
    sources = [{"label": label, "url": source_urls[label]} for label in sorted(source_urls)]
    if structured:
        sources.append({"label": "Financial tables via Screener", "url": get_snapshot(ticker).get("source_url")})
    return {"content": content, "sources": sources}


def generate_research_section(ticker: str, category: str):
    """Use exactly the same evidence for precomputed and requested sections."""
    if category == "promised_vs_delivered":
        from app.pipeline.promised_vs_delivered import build_promised_vs_delivered
        return build_promised_vs_delivered(ticker)
    structured = None
    if category in DEEP_CATEGORIES or category in {"balance_sheet", "cash_quality"}:
        snapshot = get_snapshot(ticker)
        if snapshot:
            structured = {
                "financial_snapshot": snapshot.get("profit_loss"),
                "trajectory": snapshot.get("profit_loss"),
                "balance_sheet": {"balance_sheet": snapshot.get("balance_sheet"),
                                  "ratios": snapshot.get("ratios")},
                "cash_quality": snapshot.get("cash_flow"),
            }.get(category)
    return generate_section(ticker, category, structured=structured)
