"""Flagship Pull-mode output: for each of the last four reported quarters,
compare what management guided on (from the concall that followed the
prior quarter's results) against what was actually delivered (from the
company's own reported numbers). Grounded entirely in primary sources.
"""

import logging
from datetime import datetime

import numpy as np

from app.llm.client import chat_json, embed
from app.pipeline.pull_pipeline import SYSTEM_PROMPT, build_index
from app.sources.screener_client import fetch_soup, get_documents, get_quarterly_table

logger = logging.getLogger(__name__)

GUIDANCE_QUERY = "guidance outlook growth targets margin capex plans management commentary next quarter"

VERDICT_PROMPT = """Management guidance excerpts from the concall held around {concall_label}
(discussing the quarter ended {prior_quarter}, and looking ahead to {target_quarter}):

{guidance_text}

Actual results delivered for {target_quarter} (from the company's own reported financials,
compared to {prior_quarter} = QoQ and to the same quarter a year earlier = YoY):
{actual_json}

Based ONLY on the above, did the company beat, meet, or miss what management had guided/implied?
Return JSON: {{"verdict": "beat|met|missed|unclear", "rationale": "one sentence"}}.
If the excerpts don't contain clear forward-looking guidance, return "unclear" with a rationale saying so.
"""


def _parse_period(label: str) -> datetime | None:
    try:
        return datetime.strptime(label, "%b %Y")
    except (ValueError, TypeError):
        return None


def _retrieve_within(chunks: list[dict], label: str, query: str, k: int = 5) -> list[dict]:
    subset = [c for c in chunks if c["label"] == label]
    if not subset:
        return []
    q_emb = np.array(embed([query])[0])
    mat = np.array([c["embedding"] for c in subset])
    norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(q_emb) + 1e-8
    sims = (mat @ q_emb) / norms
    top_idx = np.argsort(-sims)[:k]
    return [subset[i] for i in top_idx]


def build_promised_vs_delivered(ticker: str, n_quarters: int = 4) -> list[dict]:
    soup = fetch_soup(ticker)
    if soup is None:
        return []

    quarters_table = get_quarterly_table(soup)
    quarter_labels = list(quarters_table.keys())
    if len(quarter_labels) < n_quarters + 1:
        return []

    docs = get_documents(soup)
    concalls_with_dates = [
        {**c, "date": _parse_period(c["label"])}
        for c in docs["concalls"]
        if c.get("transcript_url") and _parse_period(c["label"])
    ]

    chunks = build_index(ticker)
    results = []

    target_labels = quarter_labels[-n_quarters:]
    for target_label in target_labels:
        i = quarter_labels.index(target_label)
        if i == 0:
            continue
        prior_label = quarter_labels[i - 1]
        prior_date = _parse_period(prior_label)
        target_date = _parse_period(target_label)
        if not prior_date or not target_date:
            continue

        candidates = [c for c in concalls_with_dates if prior_date < c["date"] <= target_date]
        guidance_concall = min(candidates, key=lambda c: c["date"]) if candidates else None

        actual = {
            metric: quarters_table[target_label].get(metric)
            for metric in ("revenue", "ebitda", "pbt", "pat")
        }
        prior_actual = {
            metric: quarters_table[prior_label].get(metric)
            for metric in ("revenue", "ebitda", "pbt", "pat")
        }
        actual_with_qoq = {
            m: {
                "value": actual[m],
                "qoq_pct": (
                    round((actual[m] - prior_actual[m]) / abs(prior_actual[m]) * 100, 1)
                    if actual[m] is not None and prior_actual.get(m)
                    else None
                ),
            }
            for m in actual
        }

        if guidance_concall is None:
            results.append(
                {
                    "quarter": target_label,
                    "verdict": "unclear",
                    "rationale": "No concall transcript found covering the run-up to this quarter.",
                    "actual": actual_with_qoq,
                    "guidance_source": None,
                }
            )
            continue

        guidance_chunks = _retrieve_within(chunks, f"Concall {guidance_concall['label']}", GUIDANCE_QUERY)
        guidance_text = "\n\n".join(c["text"] for c in guidance_chunks) or "(no relevant excerpt found)"

        prompt = VERDICT_PROMPT.format(
            concall_label=guidance_concall["label"],
            prior_quarter=prior_label,
            target_quarter=target_label,
            guidance_text=guidance_text,
            actual_json=actual_with_qoq,
        )
        try:
            parsed = chat_json(prompt, tier="reasoning", system=SYSTEM_PROMPT)
        except Exception as e:
            logger.warning("Promised-vs-delivered verdict failed for %s/%s: %s", ticker, target_label, e)
            parsed = {"verdict": "unclear", "rationale": "Couldn't generate a verdict right now."}

        results.append(
            {
                "quarter": target_label,
                "verdict": parsed.get("verdict", "unclear"),
                "rationale": parsed.get("rationale", ""),
                "actual": actual_with_qoq,
                "guidance_source": guidance_concall["label"],
                "guidance_source_url": guidance_concall["transcript_url"],
            }
        )

    return list(reversed(results))  # most recent quarter first
