"""Push-side enrichment: management-change bio cards and QoQ/YoY results
cards. Enrichment is explicitly allowed to use public web sources (unlike
Pull mode, which is primary-source-only)."""

import logging

from app.llm.client import chat_json, web_search_answer
from app.sources.screener_client import get_qoq_yoy

logger = logging.getLogger(__name__)

EXTRACT_PERSON_PROMPT = """This is an NSE corporate announcement headline about a management change:

"{headline}"

Extract the person's name and the role/title they are joining, resigning, or
retiring from. Return JSON: {{"name": "...", "role": "...", "action": "joining|resigning|retiring|other"}}.
If no specific person is named, return {{"name": null, "role": null, "action": "other"}}.
"""

BIO_PROMPT = """{name} has just {action} as {role} at {company}, an NSE-listed Indian company.
Search the web for their public professional background and write a 2-3 sentence bio
covering prior roles/companies and relevant experience. If you can't find reliable public
information about this specific person, say so plainly instead of guessing."""


def extract_person(headline: str) -> dict:
    try:
        return chat_json(EXTRACT_PERSON_PROMPT.format(headline=headline), tier="cheap")
    except Exception as e:
        logger.warning("Person extraction failed: %s", e)
        return {"name": None, "role": None, "action": "other"}


def get_management_bio(headline: str, company: str) -> dict:
    person = extract_person(headline)
    if not person.get("name"):
        return {"person": None, "bio": None}

    bio = web_search_answer(
        BIO_PROMPT.format(
            name=person["name"],
            action=person.get("action", "joined"),
            role=person.get("role", "a leadership role"),
            company=company,
        )
    )
    return {"person": person, "bio": bio}


def get_results_card(ticker: str) -> dict | None:
    return get_qoq_yoy(ticker)
