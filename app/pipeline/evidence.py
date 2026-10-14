"""Validate source identifiers and render links from supplied evidence only."""

import re
from urllib.parse import quote

from app.presentation import source_url


def grounded_result(parsed: dict, sources: list[dict]) -> dict:
    content = parsed.get("answer") or parsed.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty grounded answer")
    known = {s["id"]: s for s in sources}
    cited = set(re.findall(r"\[([SAB]\d+)\]", content))
    claimed = parsed.get("source_ids", [])
    if not isinstance(claimed, list) or any(not isinstance(s, str) for s in claimed):
        raise ValueError("Invalid source references")
    if (cited | set(claimed)) - known.keys():
        raise ValueError("Unknown source reference")
    supported = parsed.get("supported", True)
    if supported and not cited:
        raise ValueError("A factual answer must cite evidence")
    if not supported:
        return {"content": "The available sources do not contain enough information to answer this reliably.",
                "sources": [], "supported": False}
    used = [s for s in sources if s["id"] in cited]
    # Only server-supplied URLs become citation links; model-produced URLs are unnecessary.
    content = re.sub(r"\[([SAB]\d+)\]", lambda m: (
        f"[{m.group(0)}]({quote(known[m[1]]['url'], safe=':/?&=#%')})"
        if source_url(known[m[1]].get("url")) else m.group(0)), content)
    return {"content": content, "sources": used, "supported": True}
