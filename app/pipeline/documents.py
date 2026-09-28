"""Download + extract + chunk primary-source PDFs (annual reports, concall
transcripts) for the Pull-mode retrieval pipeline. Downloads are cached to
disk by URL hash so repeat runs (including the video recording) don't
re-fetch or re-parse the same document."""

import hashlib
import logging
from pathlib import Path

import fitz  # PyMuPDF
import requests

from app.config import DATA_DIR
from app.storage import atomic_write

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "pdf_cache"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}

CHUNK_WORDS = 350
CHUNK_OVERLAP_WORDS = 50


def _cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:24]
    return CACHE_DIR / f"{h}.pdf"


def download_pdf(url: str) -> bytes | None:
    path = _cache_path(url)
    if path.exists():
        return path.read_bytes()

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            logger.warning("Not a PDF response for %s", url)
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(path, resp.content)
        return resp.content
    except requests.RequestException as e:
        logger.warning("PDF download failed for %s: %s", url, e)
        return None


def extract_text(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n\n".join(pages)


def fetch_and_extract(url: str) -> str | None:
    pdf_bytes = download_pdf(url)
    if pdf_bytes is None:
        return None
    return extract_text(pdf_bytes)


def chunk_text(text: str, chunk_words: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = []
    step = max(chunk_words - overlap, 1)
    for start in range(0, len(words), step):
        chunk = " ".join(words[start : start + chunk_words])
        if chunk.strip():
            chunks.append(chunk)
        if start + chunk_words >= len(words):
            break
    return chunks
