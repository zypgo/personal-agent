"""The maintained article reader must preserve the application's privacy and fallback rules."""

from __future__ import annotations

import logging
from threading import BoundedSemaphore

import pytest
from pydantic import ValidationError

from personal_agent.config import Config, ToolsConfig
from personal_agent.tools import extraction
from personal_agent.tools.htmltext import html_to_text

BASE = "https://example.com/articles/current?private=test-value"
ARTICLE = "Reliable agents preserve permissions and recover the same durable work. " * 6
PAGE = (
    "<html><head><title>Agent architecture</title></head><body><nav>Menu</nav>"
    "<article><h1>Architecture</h1><p>" + ARTICLE + "</p>"
    '<p>Read the <a href="/guide">complete guide</a> for implementation details.</p>'
    "</article><footer>Footer navigation</footer></body></html>"
)


def test_auto_keeps_small_fragments_and_code_listings_on_the_basic_reader(monkeypatch):
    calls = []
    monkeypatch.setattr(extraction, "extract", lambda *args, **kwargs: calls.append(kwargs))
    for page in ("<p>Hello</p>", PAGE + "<PRE>def f():\n    return 1\n</PRE>"):
        assert extraction.extract_html(page, BASE) == html_to_text(page, BASE)
    assert not calls


def test_library_selects_content_but_existing_formatter_controls_links_and_images(monkeypatch):
    calls = []

    def article_reader(html, **kwargs):
        calls.append((html, kwargs))
        return (
            f"<article><p>{ARTICLE}</p>"
            '<a href="/guide">Guide</a><a href="javascript:alert(1)">bad</a>'
            '<img src="https://tracker.example/pixel" alt="image"></article>'
        )

    monkeypatch.setattr(extraction, "extract", article_reader)
    result = extraction.extract_html(PAGE, BASE, backend="trafilatura")
    assert result.startswith("Title: Agent architecture\n\n")
    assert ARTICLE.strip() in result
    assert "[Guide](https://example.com/guide)" in result
    assert "javascript:" not in result and "tracker.example" not in result
    assert calls[0][0] == PAGE
    options = calls[0][1]
    assert options["url"] is None
    assert options["include_comments"] is False
    assert options["include_images"] is False
    assert options["with_metadata"] is False
    assert options["deduplicate"] is False


@pytest.mark.parametrize("selected", [None, "", "<p>too little</p>", ["not a string"]])
def test_unusable_extraction_preserves_the_original_readable_page(monkeypatch, selected):
    monkeypatch.setattr(extraction, "extract", lambda *args, **kwargs: selected)
    assert extraction.extract_html(PAGE, BASE, backend="trafilatura") == html_to_text(PAGE, BASE)


def test_parser_failure_does_not_log_page_credentials_or_exception_details(monkeypatch, caplog):
    secret = "private-page-value-should-never-be-logged"

    def broken(*args, **kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(extraction, "extract", broken)
    with caplog.at_level(logging.DEBUG, logger="personal_agent.tools.extraction"):
        assert extraction.extract_html(PAGE, BASE, backend="trafilatura") == html_to_text(
            PAGE, BASE
        )
    assert secret not in caplog.text and BASE not in caplog.text


@pytest.mark.parametrize(
    "page",
    ["x" * (extraction.MAX_ARTICLE_CHARS + 1), "<b>x</b>" * extraction.MAX_ARTICLE_TAGS],
)
def test_hostile_size_or_markup_density_never_enters_the_article_parser(monkeypatch, page):
    calls = []
    monkeypatch.setattr(extraction, "extract", lambda *args, **kwargs: calls.append(kwargs))
    assert extraction.extract_html(page, BASE, backend="trafilatura") == html_to_text(page, BASE)
    assert not calls


@pytest.mark.parametrize("attribute", ['colspan="99" rowspan="99"', "ROWSPAN \n= 999999"])
def test_spanning_tables_cannot_expand_inside_the_article_parser(monkeypatch, attribute):
    page = "<table>" + f"<tr><td {attribute}>{ARTICLE}</td></tr>" * 700 + "</table>"
    assert len(page) < extraction.MAX_ARTICLE_CHARS
    assert page.count("<") < extraction.MAX_ARTICLE_TAGS
    calls = []
    monkeypatch.setattr(extraction, "extract", lambda *args, **kwargs: calls.append(kwargs))
    assert extraction.extract_html(page, BASE, backend="trafilatura") == html_to_text(page, BASE)
    assert not calls


def test_saturated_parser_capacity_falls_back_without_queueing_more_work(monkeypatch):
    monkeypatch.setattr(extraction, "_slots", BoundedSemaphore(0))
    calls = []
    monkeypatch.setattr(extraction, "extract", lambda *args, **kwargs: calls.append(kwargs))
    assert extraction.extract_html(PAGE, BASE, backend="trafilatura") == html_to_text(PAGE, BASE)
    assert not calls


def test_real_library_is_offline_and_does_not_deduplicate_between_spaces(monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        pytest.fail("Article extraction attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    first = extraction.extract_html(PAGE, BASE, backend="trafilatura")
    second = extraction.extract_html(PAGE, BASE, backend="trafilatura")
    assert first == second
    assert ARTICLE.strip() in first
    assert "https://example.com/guide" in first
    assert "Menu" not in first and "Footer navigation" not in first


def test_upstream_debug_logs_never_reveal_paragraph_text(caplog):
    marker = "private-paragraph-marker-should-not-reach-any-log"
    page = f"<html><body><article><p>{marker} {ARTICLE}<b>Read this</b></p></article></body></html>"
    with caplog.at_level(logging.DEBUG):
        result = extraction.extract_html(page, BASE, backend="trafilatura")
    assert marker in result
    assert marker not in caplog.text and BASE not in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_article_text_caches_are_cleared_even_after_parser_failure(monkeypatch, fails):
    from trafilatura.utils import line_processing, trim

    def reader(*args, **kwargs):
        trim(ARTICLE)
        line_processing(ARTICLE)
        if fails:
            raise ValueError("Malformed private article")
        return f"<p>{ARTICLE}</p>"

    monkeypatch.setattr(extraction, "extract", reader)
    assert ARTICLE.strip() in extraction.extract_html(PAGE, BASE, backend="trafilatura")
    assert trim.cache_info().currsize == 0
    assert line_processing.cache_info().currsize == 0


def test_config_exposes_only_supported_local_extractor_names():
    assert Config().tools.html_extractor == "auto"
    for backend in ("auto", "basic", "trafilatura"):
        assert ToolsConfig(html_extractor=backend).html_extractor == backend
    with pytest.raises(ValidationError):
        ToolsConfig(html_extractor="remote-reader")
