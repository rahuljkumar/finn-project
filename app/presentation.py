"""Safe formatting for generated research and its document links."""

from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markupsafe import Markup


_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


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
