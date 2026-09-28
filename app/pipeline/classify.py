"""Classify raw NSE/BSE announcements into the 17-category scheme from
AI_Engineer_Build_Challenge_Reference.pdf, Section 1.

Three tiers, cheapest first:
  1. Exact match on NSE's own `desc` field -> category (covers the large
     majority of real announcement volume, verified against a live sample
     across the full portfolio).
  2. Keyword check against `desc` + `attchmntText` for the handful of `desc`
     values that are genuinely ambiguous on their own.
  3. Cheap-model batch classification for whatever's left (mostly vague
     descs like "General Updates" / "Updates") -- kept as a last resort so
     we're not spending API calls on the ~95% of items rules already settle.
"""

import json
import logging

from app.config import CATEGORY_PRIORITY
from app.llm.client import chat_json

logger = logging.getLogger(__name__)

CATEGORIES = list(CATEGORY_PRIORITY.keys())

EXACT_DESC_MAP = {
    "Acquisition": "M&A",
    "Scheme of Arrangement": "M&A",
    "Amalgamation/Merger": "M&A",
    "Other Restructuring": "M&A",
    "Diversification/Disinvestment": "M&A",
    "Sale or disposal": "M&A",
    "Disclosure under SEBI Takeover Regulations": "M&A",
    "Memorandum of Understanding/Agreements": "press_release",

    "Change in Management": "management_change",
    "Change in Director(s)": "management_change",
    "Appointment": "management_change",
    "Resignation of Director/KMP/SMP": "management_change",
    "Resignation": "management_change",
    "Cessation": "management_change",

    "Allotment of Securities": "fund_raise",

    "Credit Rating": "credit_rating",
    "Credit Rating- New": "credit_rating",

    "Dividend": "dividend",

    "Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company": "litigation",
    "Action(s) taken or orders passed": "litigation",

    "Trading Plan under PIT": "insider_trading",

    "Bagging/Receiving of orders/contracts": "press_release",
    "Press Release": "press_release",
    "News Verification": "press_release",
    "Rumour Verification - Regulation 30(11)": "press_release",
    "Disclosure of material issue": "press_release",
    "Giving guarantees/indemnity/ becoming a surety for third party": "press_release",

    "Shareholders meeting": "agm_egm",
    "Investor Presentation": "investor_meetings",

    "Commencement of commercial production/operations": "capex",

    "Integrated Filing- Financial": "results",

    "ESOP/ESOS/ESPS": "unclassified",
    "Trading Window": "unclassified",
    "Certificate under SEBI (Depositories and Participants) Regulations, 2018": "unclassified",
    "Monitoring Agency Report": "unclassified",
    "Statement of deviation(s) or variation(s) under Reg. 32": "unclassified",
    "Amendment to AOA/MOA": "unclassified",
    "Corrigendum": "unclassified",
    "Committee Meeting Updates": "unclassified",
    "Address Change": "unclassified",
    "Change in Auditors": "unclassified",
    "Options to purchase securities": "unclassified",
}

AMBIGUOUS_DESCS = {
    "Outcome of Board Meeting",
    "Copy of Newspaper Publication",
    "Record Date",
    "Analysts/Institutional Investor Meet/Con. Call Updates",
}


def _classify_ambiguous(desc: str, text: str) -> str:
    t = text.lower()
    if desc == "Analysts/Institutional Investor Meet/Con. Call Updates":
        if any(k in t for k in ("con. call", "concall", "con call", "conference call", "transcript", "earnings call")):
            return "earnings_call"
        return "investor_meetings"
    if desc == "Record Date":
        if "bonus" in t or "split" in t or "sub-division" in t or "subdivision" in t:
            return "bonus_split"
        return "dividend"
    if desc == "Outcome of Board Meeting":
        if "result" in t:
            return "results"
        if "dividend" in t:
            return "dividend"
        if "bonus" in t or "split" in t:
            return "bonus_split"
        if "acqui" in t or "merger" in t or "amalgamat" in t or "joint venture" in t:
            return "M&A"
        if "rights issue" in t or "qip" in t or "preferential" in t or "buyback" in t:
            return "fund_raise"
        return "results"
    if desc == "Copy of Newspaper Publication":
        if "result" in t:
            return "results"
        if "dividend" in t:
            return "dividend"
        if "postal ballot" in t:
            return "postal_ballot"
        if "agm" in t or "egm" in t or "annual general meeting" in t:
            return "agm_egm"
        return "press_release"
    return "unclassified"


def rule_classify(announcement: dict) -> str | None:
    desc = (announcement.get("desc") or "").strip().removesuffix("-XBRL").strip()
    if desc in AMBIGUOUS_DESCS:
        text = f"{desc} {announcement.get('attchmntText', '')}"
        return _classify_ambiguous(desc, text)
    if desc in EXACT_DESC_MAP:
        return EXACT_DESC_MAP[desc]
    return None


LLM_CLASSIFY_PROMPT = """Classify each corporate announcement below into exactly one of these categories:
{categories}

Category definitions:
- results: quarterly/annual results, board meeting approving them, results decks
- M&A: acquisition, merger, demerger, joint venture, stake sale in a subsidiary
- fund_raise: rights issue, QIP, preferential allotment, buyback, debt raise
- management_change: CEO/CFO/MD/director joining, resigning, retiring
- litigation: lawsuits, regulatory orders, fines, insolvency proceedings
- insider_trading: promoter/insider buying/selling personal shareholding
- earnings_call: concall notice, transcript, or recording link
- investor_meetings: one-on-one/group investor meetings or conferences
- credit_rating: rating assigned/upgraded/downgraded/reaffirmed
- dividend: cash dividend declared, with record date
- bonus_split: bonus issue or stock split
- capex: approving spend on new plant/facility/capacity
- press_release: general news not covered elsewhere (order wins, products, partnerships, rumour clarification)
- postal_ballot: postal ballot notice or result
- agm_egm: AGM/EGM notice or result
- pledging: promoter share pledge changes
- unclassified: routine/low-relevance paperwork (ESOP, compliance certs, ESG, auditor appointments)

Return a JSON object of the form {{"results": [{{"id": ..., "category": ...}}, ...]}},
one entry per input item, in the same order.

Items:
{items}
"""


def classify_batch(announcements: list[dict]) -> dict[str, str]:
    """Classify a batch of announcements, id -> category. Applies rules first,
    only sends the unresolved remainder to the cheap model in one batched call."""
    result: dict[str, str] = {}
    unresolved = []
    for a in announcements:
        aid = str(a.get("seq_id") or a.get("id") or id(a))
        cat = rule_classify(a)
        if cat:
            result[aid] = cat
        else:
            unresolved.append((aid, a))

    if not unresolved:
        return result

    items_payload = [
        {
            "id": aid,
            "desc": a.get("desc", ""),
            "text": (a.get("attchmntText") or "")[:300],
        }
        for aid, a in unresolved
    ]
    prompt = LLM_CLASSIFY_PROMPT.format(
        categories=", ".join(CATEGORIES),
        items=json.dumps(items_payload, ensure_ascii=False),
    )
    try:
        parsed = chat_json(prompt, tier="cheap")
        rows = parsed if isinstance(parsed, list) else parsed.get("results", [])
        for row in rows:
            cat = row.get("category")
            if cat in CATEGORIES:
                result[str(row["id"])] = cat
    except Exception as e:
        logger.warning("LLM classification fallback failed: %s", e)

    # Anything still unresolved (LLM call failed, or returned partial) -> unclassified
    for aid, _ in unresolved:
        result.setdefault(aid, "unclassified")

    return result
