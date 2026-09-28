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
