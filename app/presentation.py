"""Safe formatting for generated research and its document links."""

from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

from markdown_it import MarkdownIt
from markupsafe import Markup


_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def display_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %H:%M IST")
    except ValueError:
        return value


def render_markdown(value: str | None) -> Markup:
    # Generated text is untrusted: never allow raw HTML or unsafe link schemes.
    return Markup(_markdown.render(value or ""))


def source_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None
