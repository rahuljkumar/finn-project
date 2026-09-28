"""Thin OpenAI wrapper with cheap/reasoning model routing and a
content-hash cache in front of every call.

Model tiers are read from env (CHEAP_MODEL / REASONING_MODEL) rather than
hardcoded -- check your OpenAI dashboard for current model slugs before
running this for real; naming changes over time and this repo shouldn't
guess at it.
"""

import hashlib
import json
import logging

from openai import OpenAI

from app.config import CHEAP_MODEL, EMBEDDING_MODEL, OPENAI_API_KEY, REASONING_MODEL
from app.llm.cache import get_cached, set_cached

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY, timeout=90.0, max_retries=1)
    return _client


def _model_for(tier: str) -> str:
    return CHEAP_MODEL if tier == "cheap" else REASONING_MODEL


def _cache_key(*parts: str) -> str:
    payload = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_messages(prompt: str, system: str | None) -> list[dict]:
    # Static/system content first, variable prompt last -- lets OpenAI's
    # automatic prefix caching kick in when `system` repeats across calls.
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def chat_text(prompt: str, tier: str = "reasoning", system: str | None = None) -> str:
    model = _model_for(tier)
    messages = _build_messages(prompt, system)
    key = _cache_key("text", model, json.dumps(messages, sort_keys=True))

    cached = get_cached(key)
    if cached is not None:
        return cached

    resp = get_client().chat.completions.create(model=model, messages=messages)
    text = resp.choices[0].message.content or ""
    set_cached(key, text, model)
    return text


def chat_json(prompt: str, tier: str = "reasoning", system: str | None = None) -> dict:
    model = _model_for(tier)
    messages = _build_messages(prompt, system)
    key = _cache_key("json", model, json.dumps(messages, sort_keys=True))

    cached = get_cached(key)
    if cached is not None:
        return json.loads(cached)

    resp = get_client().chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or "{}"
    parsed = json.loads(text)
    set_cached(key, text, model)
    return parsed


def web_search_answer(prompt: str, tier: str = "reasoning") -> str:
    """Answer grounded in a live web search via the Responses API's hosted
    web_search tool. Used only for Push-side enrichment (e.g. a new
    manager's public background) -- Pull mode must never use this."""
    model = _model_for(tier)
    key = _cache_key("websearch", model, prompt)

    cached = get_cached(key)
    if cached is not None:
        return cached

    resp = get_client().responses.create(
        model=model,
        input=prompt,
        tools=[{"type": "web_search"}],
    )
    text = resp.output_text
    set_cached(key, text, model)
    return text


def search_filings(prompt: str, domains: list[str], model: str) -> dict:
    """Uncached discovery: the Digest collector controls its durable cooldown.

    Return provider provenance alongside JSON. URLs must subsequently pass
    document verification or an explicit provider-open check.
    """
    fields = {name: {"type": "string"} for name in (
        "ticker", "source_url", "published_date", "headline",
        "company_evidence", "date_evidence", "content_evidence",
    )}
    resp = get_client().with_options(timeout=120.0, max_retries=0).responses.create(
        model=model,
        input=prompt,
        tools=[{"type": "web_search", "filters": {"allowed_domains": domains}}],
        tool_choice="required",
        include=["web_search_call.action.sources"],
        max_tool_calls=8,
        parallel_tool_calls=False,
        max_output_tokens=7000,
        text={"format": {
            "type": "json_schema", "name": "corporate_filings", "strict": True,
            "schema": {"type": "object", "properties": {"filings": {
                "type": "array", "items": {"type": "object", "properties": fields,
                                            "required": list(fields), "additionalProperties": False},
            }}, "required": ["filings"], "additionalProperties": False},
        }},
    )
    if resp.status != "completed":
        raise ValueError("Filing search did not complete")
    sources, opened, searched = set(), set(), False
    for output in resp.output:
        data = output.model_dump()
        if data.get("type") == "web_search_call" and data.get("status") == "completed":
            searched = True
            action = data.get("action") or {}
            sources.update(s["url"] for s in (action.get("sources") or []) if s.get("url"))
            if action.get("type") == "open_page" and action.get("url"):
                opened.add(action["url"])
                sources.add(action["url"])
        if data.get("type") == "message":
            for part in (data.get("content") or []):
                sources.update(a["url"] for a in (part.get("annotations") or [])
                               if a.get("type") == "url_citation" and a.get("url"))
    if not searched:
        raise ValueError("Filing search returned no web provenance")
    payload = json.loads(resp.output_text)
    if not isinstance(payload, dict) or not isinstance(payload.get("filings"), list):
        raise ValueError("Invalid filing search response")
    return {"filings": payload["filings"], "sources": sorted(sources), "opened": sorted(opened)}


EMBED_BATCH_SIZE = 100  # keep well under the embeddings endpoint's per-request token/item limits


def embed(texts: list[str]) -> list[list[float]]:
    results: list[list[float] | None] = [None] * len(texts)
    to_fetch: list[tuple[int, str]] = []

    for i, t in enumerate(texts):
        key = _cache_key("embed", EMBEDDING_MODEL, t)
        cached = get_cached(key)
        if cached is not None:
            results[i] = json.loads(cached)
        else:
            to_fetch.append((i, t))

    for batch_start in range(0, len(to_fetch), EMBED_BATCH_SIZE):
        batch = to_fetch[batch_start : batch_start + EMBED_BATCH_SIZE]
        resp = get_client().embeddings.create(model=EMBEDDING_MODEL, input=[t for _, t in batch])
        for (i, t), item in zip(batch, resp.data):
            results[i] = item.embedding
            set_cached(_cache_key("embed", EMBEDDING_MODEL, t), json.dumps(item.embedding), EMBEDDING_MODEL)

    return results  # type: ignore[return-value]
