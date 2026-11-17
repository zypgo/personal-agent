"""Bounded, local-only article extraction behind the existing safe HTML formatter.

Trafilatura selects article content; our formatter keeps the established URL and image rules.
Neither adapter fetches URLs. Download limits, DNS checks and redirects belong to the egress
gate. The basic reader remains the fallback for fragments, very large pages and parser failures.
"""

from __future__ import annotations

import logging
import re
from configparser import ConfigParser
from threading import BoundedSemaphore
from typing import Literal

from trafilatura import extract
from trafilatura.meta import reset_caches
from trafilatura.settings import DEFAULT_CONFIG

from personal_agent.tools.htmltext import html_to_text

Extractor = Literal["auto", "basic", "trafilatura"]
MAX_ARTICLE_CHARS = 512_000
MAX_ARTICLE_TAGS = 5_000
MIN_ARTICLE_CHARS = 120
AUTO_MIN_HTML_CHARS = 4_096
TABLE_SPAN = re.compile(r"\b(?:rowspan|colspan)\s*=", re.IGNORECASE)
_slots = BoundedSemaphore(2)
log = logging.getLogger(__name__)
# Upstream DEBUG messages can contain whole paragraphs, and even warning/error messages can
# echo malformed input. Keep that branch out of application/root handlers at every log level.
# Do not temporarily change levels per request: extraction runs concurrently across spaces.
_upstream_log = logging.getLogger("trafilatura")
_upstream_log.addHandler(logging.NullHandler())
_upstream_log.propagate = False
_PARSER_LOG_ROOTS = {"trafilatura", "htmldate", "courlan", "justext", "charset_normalizer"}


def _silence_parser_logs() -> None:
    # Disable at the source too: instrumentation may install a handler directly on a child
    # logger, bypassing its parent's propagation setting. These libraries are used only for
    # private parsing here. Keep this setting stable across overlapping calls, never restore it.
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.partition(".")[0] in _PARSER_LOG_ROOTS:
            logging.getLogger(name).disabled = True


def _settings() -> ConfigParser:
    """Each extraction gets its own options; no cross-space mutable cache or configuration."""
    config = ConfigParser()
    config.read_dict({"DEFAULT": dict(DEFAULT_CONFIG.defaults())})
    config.set("DEFAULT", "MAX_FILE_SIZE", str(MAX_ARTICLE_CHARS * 4))
    config.set("DEFAULT", "MAX_TREE_SIZE", str(MAX_ARTICLE_TAGS))
    return config


def extract_html(html: str, base_url: str = "", *, backend: Extractor = "auto") -> str:
    """Read already-downloaded HTML without a network request or shared deduplication state.

    Use the linear basic reader for oversized/markup-heavy input. A bounded number of article
    parsers may run concurrently, including ones whose async caller has already been cancelled.
    Failures never log page content, URLs or exception messages, which may contain credentials.
    """
    if backend not in ("auto", "basic", "trafilatura"):
        raise ValueError("Unknown HTML extractor")
    basic = html_to_text(html, base_url)
    if (
        backend == "basic"
        or len(html) > MAX_ARTICLE_CHARS
        or html.count("<") > MAX_ARTICLE_TAGS
        # Article heuristics may turn preformatted code into quotations. Keep the proven
        # whitespace-preserving path for source listings instead of damaging runnable examples.
        or "<pre" in html.lower()
        # A small source table can expand to millions of internal cells before upstream's
        # tree-size check. The basic formatter does not expand spans or allocate that grid.
        or TABLE_SPAN.search(html) is not None
        or (backend == "auto" and len(html) < AUTO_MIN_HTML_CHARS)
    ):
        return basic
    if not _slots.acquire(blocking=False):
        return basic
    try:
        _silence_parser_logs()
        selected = extract(
            html,
            # Relative URLs are resolved later by our formatter. The article library never
            # receives the source URL (which can contain query credentials) for its own logs.
            url=None,
            output_format="html",
            include_comments=False,
            include_tables=True,
            include_formatting=True,
            include_links=True,
            include_images=False,
            with_metadata=False,
            deduplicate=False,
            config=_settings(),
        )
        if not isinstance(selected, str) or not selected or len(selected) > MAX_ARTICLE_CHARS * 4:
            return basic
        # Formatting and permitted link schemes remain identical to the basic reader. Never
        # feed library-generated HTML directly to a browser or render external images.
        text = html_to_text(selected, base_url).strip()
        if len(text) < MIN_ARTICLE_CHARS:
            return basic
        title, separator, _ = basic.partition("\n\n")
        return f"{title}\n\n{text}" if separator and title.startswith("Title: ") else text
    except Exception:
        log.debug("Article extraction failed; using the basic reader")
        return basic
    finally:
        try:
            # deduplicate=False disables article deduplication but upstream pure-function LRUs
            # still retain text. The official reset hook clears its own and dependency caches;
            # the last concurrent extraction also clears any entries left by earlier work.
            reset_caches()
        finally:
            _slots.release()
