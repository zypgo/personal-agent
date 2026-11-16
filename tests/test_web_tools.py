"""The web tools inside real runs: what the model sees, what is refused, what needs the owner,
and that outside content and credentials stay where they belong."""

from __future__ import annotations

import json
import socket

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from openai.types.responses import ResponseFunctionWebSearch
from openai.types.responses.response_function_web_search import (
    ActionOpenPage,
    ActionSearch,
    ActionSearchSource,
)

from helpers import owner_message, usage, webhook
from personal_agent.config import (
    Config,
    ModelConfig,
    NetConfig,
    PolicyConfig,
    SecretConfig,
    ToolsConfig,
)
from personal_agent.tools.base import Category
from tooling import drain, outputs

TOKEN = "ghp_" + "Zx9Y8w7V" * 4


def cfg(*, secrets=None, allow_private=("127.0.0.1",), tools=None, model=None, **policy) -> Config:
    return Config(
        policy=PolicyConfig(**policy),
        net=NetConfig(allow_private=list(allow_private)),
        secrets=secrets or {},
        tools=tools or ToolsConfig(),
        model=model or ModelConfig(),
    )


def fetch(call_id: str, url: str, **kw):
    return function_call("web_fetch", {"url": url, **kw}, call_id=call_id)


def req(call_id: str, url: str, **kw):
    return function_call("http_request", {"url": url, **kw}, call_id=call_id)


def search(query: str, *sources: str, call_id: str = "ws_1"):
    return ResponseFunctionWebSearch(
        id=call_id,
        status="completed",
        type="web_search_call",
        action=ActionSearch(
            type="search",
            query=query,
            sources=[ActionSearchSource(type="url", url=u) for u in sources],
        ),
    )


async def run(app, text="go"):
    app.inbox.add(owner_message(text))
    return await drain(app)


def tools_of(model: ScriptedModel, call: int = 0) -> dict:
    return {t.name: t for t in model.calls[call].tools}


def audit_for(app, tool: str):
    return list(reversed(app.audit.recent(tool=tool)))


# ---- reading pages -------------------------------------------------------------------------------------------
async def test_a_page_is_read_as_text_inside_an_untrusted_fence(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/page")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)

    out = outputs(app)["c1"]
    assert result.outcome.status == "completed"
    assert out.startswith(f"URL: {site.base}/page\nStatus: 200\nContent-Type: text/html")
    assert '<untrusted source="web:127.0.0.1">' in out and out.rstrip().endswith("</untrusted>")
    assert "Title: Test page" in out and "# Hello world" in out and "- one" in out
    assert f"[docs]({site.base}/docs)" in out
    assert not any(word in out for word in ("Menu", "copyright", "alert"))


async def test_reading_a_page_marks_the_run_as_having_read_outside_content(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/text")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)

    assert app.runs.get(result.outcome.run_id).tainted
    [row] = app.audit.recent()
    assert (row.tool, row.category, row.verdict, row.outcome) == (
        "web_fetch",
        "read",
        "allow",
        "ok",
    )
    assert row.detail == f"{site.base}/text"


async def test_a_page_that_tries_to_give_orders_is_only_ever_shown_as_data(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/injection")], [assistant_message("noted")]])
    app = make_app(model, config=cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert (
        out.index("<untrusted")
        < out.index("IGNORE ALL PREVIOUS INSTRUCTIONS")
        < out.index("</untrusted>")
    )


async def test_text_and_other_charsets_are_decoded(make_app, site):
    model = ScriptedModel(
        [
            [fetch("a", f"{site.base}/text"), fetch("b", f"{site.base}/latin1")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    await run(app)
    assert "plain words" in outputs(app)["a"] and "café" in outputs(app)["b"]


async def test_a_long_page_is_cut_at_the_requested_length_and_says_so(make_app, site):
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/big", max_chars=500)], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert "more characters not shown" in out and len(out) < 1000
    assert out.rstrip().endswith("</untrusted>")


async def test_the_default_length_never_overflows_the_tool_output_limit(make_app, site):
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/big", max_chars=1_000_000)], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert "omitted]" not in out  # the guard did not have to cut it again
    assert out.rstrip().endswith("</untrusted>")
    assert len(out) <= app.config.runtime.tool_output_chars


async def test_a_redirect_is_followed_and_reported(make_app, site):
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/redirect?to=/text&code=301")], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert f"Redirected to: {site.base}/text" in out and "plain words" in out


async def test_pages_that_are_not_text_are_reported_without_their_content(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/image")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    out = outputs(app)["c1"]
    assert out.startswith("Error:") and "image/png" in out and "cannot be read as text" in out
    assert not app.runs.get(result.outcome.run_id).tainted  # nothing was shown


async def test_an_error_page_shows_the_status_but_none_of_the_server_text(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/status/404")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    out = outputs(app)["c1"]
    assert out.startswith("Error: the server answered HTTP 404") and "it said no" not in out
    assert not app.runs.get(result.outcome.run_id).tainted
    assert audit_for(app, "web_fetch")[0].outcome == "error"


async def test_a_server_that_cannot_be_reached_is_an_ordinary_error(make_app, site):
    dead = site.base
    site.stop()
    model = ScriptedModel([[fetch("c1", f"{dead}/page")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert (
        outputs(app)["c1"].startswith("Error: could not fetch")
        and result.outcome.status == "completed"
    )


# ---- addresses that can never work are refused before anyone is asked ------------------------------------
@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://localhost:{port}/page", "localhost cannot be reached"),
        ("http://LOCALHOST./page", "localhost cannot be reached"),
        ("http://api.localhost/page", "localhost cannot be reached"),
        ("http://[::1]:{port}/", "allow_private"),
        ("http://[::ffff:7f00:1]:{port}/", "allow_private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://10.0.0.5/admin", "allow_private"),
        ("http://192.168.1.1/", "allow_private"),
        ("file:///etc/passwd", "only http"),
        ("ftp://example.com/x", "only http"),
        ("http://user:pw@example.com/", "user name"),
        ("not a url", "only http"),
    ],
)
async def test_addresses_that_can_never_work_are_refused_without_a_network_request(
    make_app, site, url, why
):
    model = ScriptedModel([[fetch("c1", url.format(port=site.port))], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)

    assert why in outputs(app)["c1"] and outputs(app)["c1"].startswith("Error:")
    assert result.outcome.status == "completed" and site.seen == []
    [row] = app.audit.recent()
    assert (row.verdict, row.layer, row.outcome) == ("deny", "invalid", "invalid")
    assert app.approvals.pending() == []  # nobody is asked about something that cannot work


async def test_loopback_is_refused_unless_the_owner_listed_it(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/text")], [assistant_message("ok")]])
    app = make_app(model, config=cfg(allow_private=()))
    await run(app)
    assert "allow_private" in outputs(app)["c1"] and site.seen == []


async def test_a_private_address_the_owner_listed_works(make_app, site):
    model = ScriptedModel([[fetch("c1", f"{site.base}/text")], [assistant_message("ok")]])
    app = make_app(model, config=cfg(allow_private=("127.0.0.0/8",)))
    await run(app)
    assert "plain words" in outputs(app)["c1"]


async def test_a_redirect_into_a_forbidden_address_is_stopped_and_reported(make_app, site):
    target = "http://169.254.169.254/latest/meta-data/"
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/redirect?to={target}")], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    await run(app)
    assert outputs(app)["c1"].startswith("Error:") and "link-local" in outputs(app)["c1"]
    assert len(site.seen) == 1


async def test_an_api_call_that_asks_for_json_is_not_also_asking_for_html(make_app, site):
    """The tool sends its own `Accept` for pages; one the model writes must replace it, not join it."""
    call = req("c1", f"{site.base}/json", headers=[{"name": "Accept", "value": "application/json"}])
    app = make_app(ScriptedModel([[call], [assistant_message("ok")]]), config=cfg())
    await run(app)
    assert [v for n, v in site.seen[0].lines if n == "accept"] == ["application/json"]


# ---- http_request -------------------------------------------------------------------------------------------
async def test_an_api_answer_is_pretty_printed_and_fenced(make_app, site):
    model = ScriptedModel([[req("c1", f"{site.base}/json")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    out = outputs(app)["c1"]
    assert out.startswith(f"HTTP 200 from {site.base}/json\ncontent-type: application/json")
    assert '"b": 1' in out and '"a": [' in out and "<untrusted" in out
    assert app.runs.get(result.outcome.run_id).tainted


async def test_an_api_error_is_a_result_not_an_error_and_still_counts_as_outside_content(
    make_app, site
):
    model = ScriptedModel([[req("c1", f"{site.base}/status/403")], [assistant_message("ok")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    out = outputs(app)["c1"]
    assert out.startswith(f"HTTP 403 from {site.base}/status/403") and "it said no" in out
    assert "<untrusted" in out
    assert app.runs.get(result.outcome.run_id).tainted
    assert audit_for(app, "http_request")[0].outcome == "ok"


async def test_a_head_request_returns_only_the_status_and_headers(make_app, site):
    model = ScriptedModel(
        [[req("c1", f"{site.base}/text", method="HEAD")], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert (
        out.startswith("HTTP 200") and "content-type: text/plain" in out and "<untrusted" not in out
    )


async def test_a_binary_answer_is_summarized_and_an_empty_one_says_so(make_app, site):
    model = ScriptedModel(
        [
            [req("a", f"{site.base}/image"), req("b", f"{site.base}/empty")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    await run(app)
    assert "binary body" in outputs(app)["a"] and "bytes" in outputs(app)["a"]
    assert "(empty body)" in outputs(app)["b"]


async def test_a_post_asks_the_owner_and_nothing_is_sent_until_they_agree(make_app, site):
    call = req(
        "c1",
        f"{site.base}/echo",
        method="POST",
        headers=[{"name": "Content-Type", "value": "application/json"}],
        body='{"hello": "world"}',
    )
    model = ScriptedModel([[call], [assistant_message("posted")]])
    app = make_app(model, config=cfg())
    [first] = await run(app)

    assert first.outcome.status == "awaiting_approval" and site.seen == []
    [approval] = app.approvals.pending()
    assert (approval.tool, approval.summary) == (
        "http_request",
        f"POST {site.base}/echo | headers: Content-Type: application/json"
        ' | body (18 characters): {"hello": "world"}',
    )  # what would be sent is what the owner reads, not only where
    [row] = app.audit.recent()
    assert (row.category, row.verdict, row.layer) == ("net_write", "ask", "mode")

    app.approvals.decide(approval.id, True, by="owner")
    [second] = await drain(app)
    assert second.outcome.status == "completed"
    [seen] = site.seen
    assert (seen.method, seen.body, seen.headers["content-type"]) == (
        "POST",
        b'{"hello": "world"}',
        "application/json",
    )
    assert (
        json.loads(
            outputs(app)["c1"].split("<untrusted")[1].split(">", 1)[1].rsplit("</untrusted", 1)[0]
        )["method"]
        == "POST"
    )


async def test_an_approved_post_cannot_be_redirected_with_its_data_to_another_address(
    make_app, site, other_site
):
    hop = f"{site.base}/redirect?to={other_site.base}/echo&code=307"
    model = ScriptedModel(
        [[req("c1", hop, method="POST", body="private data")], [assistant_message("done")]]
    )
    app = make_app(model, config=cfg(allow=["http_request"]))
    await run(app)
    assert "redirected a request that carries data" in outputs(app)["c1"]
    assert other_site.seen == []


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"headers": [{"name": "Bad Header", "value": "x"}]}, "not a valid header name"),
        ({"headers": [{"name": "Host", "value": "evil.example"}]}, "set automatically"),
        ({"headers": [{"name": "Content-Length", "value": "5"}]}, "set automatically"),
        ({"method": "GET", "body": "x"}, "has no body"),
        ({"method": "POST", "body": "x" * 1_000_001}, "over 1000000 bytes"),
    ],
)
async def test_malformed_requests_are_refused_with_the_reason(make_app, site, args, why):
    model = ScriptedModel([[req("c1", f"{site.base}/echo", **args)], [assistant_message("ok")]])
    app = make_app(model, config=cfg(allow=["http_request"]))
    await run(app)
    assert outputs(app)["c1"].startswith("Error:") and why in outputs(app)["c1"] and site.seen == []


# ---- what is offered -------------------------------------------------------------------------------------------
async def test_untrusted_runs_get_no_web_tools(make_app, site):
    model = ScriptedModel([[assistant_message("noted")]])
    app = make_app(model, config=cfg())
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert not {"web_fetch", "http_request", "web_search"} & set(tools_of(model))


@pytest.mark.parametrize(
    ("tools", "missing"),
    [
        (ToolsConfig(web_fetch=False), "web_fetch"),
        (ToolsConfig(http_request=False), "http_request"),
        (ToolsConfig(web_search="off"), "web_search"),
    ],
)
async def test_each_web_tool_can_be_switched_off(make_app, tools, missing):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg(tools=tools))
    await run(app)
    present = set(tools_of(model))
    assert missing not in present
    assert {"web_fetch", "http_request", "web_search"} - {missing} <= present


async def test_deny_rules_remove_web_tools_from_the_offer(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg(deny=["web_fetch", "web_search"]))
    await run(app)
    assert {"web_fetch", "web_search"}.isdisjoint(tools_of(model)) and "http_request" in tools_of(
        model
    )


async def test_hosted_search_is_only_offered_on_the_responses_api(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg(model=ModelConfig(api="chat_completions")))
    await run(app)
    assert "web_search" not in tools_of(model) and "web_fetch" in tools_of(model)


async def test_cached_search_mode_does_not_open_live_pages(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg(tools=ToolsConfig(web_search="cached")))
    await run(app)
    assert tools_of(model)["web_search"].external_web_access is False


async def test_live_search_mode_leaves_the_default_in_place(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg())
    await run(app)
    assert tools_of(model)["web_search"].external_web_access is None


async def test_sources_of_hosted_searches_are_requested_from_the_api(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg())
    await run(app)
    assert "web_search_call.action.sources" in model.calls[0].model_settings.response_include


async def test_the_sources_are_not_requested_when_search_is_off(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg(tools=ToolsConfig(web_search="off")))
    await run(app)
    assert "web_search_call.action.sources" not in (
        model.calls[0].model_settings.response_include or []
    )


async def test_the_model_is_told_which_secrets_exist_and_where_they_work(make_app, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel([[assistant_message("hi")]])
    secrets = {"gh": SecretConfig(env="GH_TOKEN", hosts=["api.github.com"])}
    app = make_app(model, config=cfg(secrets=secrets))
    await run(app)
    description = tools_of(model)["http_request"].description
    assert "{{secret:gh}} (only for api.github.com)" in description and TOKEN not in description


# ---- egress: outside content must not be able to send your data out through an address ----------------------
async def test_after_reading_a_page_an_address_the_model_made_up_needs_approval(make_app, site):
    leak = f"{site.base}/collect?data=the-owners-notes"
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/injection")], [fetch("c2", leak)], [assistant_message("done")]]
    )
    app = make_app(model, config=cfg())
    [first] = await run(app)

    assert first.outcome.status == "awaiting_approval"
    assert site.paths() == ["/injection"]  # the request carrying the data was not made
    [approval] = app.approvals.pending()
    assert (approval.tool, approval.summary) == ("web_fetch", leak)
    row = audit_for(app, "web_fetch")[-1]
    assert (row.category, row.verdict, row.layer, row.tainted) == (
        "net_write",
        "ask",
        "taint",
        True,
    )

    app.approvals.decide(approval.id, True, by="owner")  # the owner can still say yes
    await drain(app)
    assert site.paths() == ["/injection", "/collect?data=the-owners-notes"]


async def test_the_owner_can_refuse_it(make_app, site):
    leak = f"{site.base}/collect?data=x"
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", leak)],
            [assistant_message("ok, not doing that")],
        ]
    )
    app = make_app(model, config=cfg())
    await run(app)
    app.approvals.decide(app.approvals.pending()[0].id, False, by="owner")
    await drain(app)
    assert site.paths() == ["/injection"]
    assert "owner declined this action" in outputs(app)["c2"]


async def test_a_header_to_an_address_the_run_has_seen_is_a_way_out_too(make_app, site):
    """The address is one the run read; the page's owner reads the headers of the request."""
    page = f"{site.base}/injection"
    notes = [{"name": "x-notes", "value": "THE-OWNERS-PRIVATE-NOTES"}]
    model = ScriptedModel(
        [
            [fetch("c1", page)],
            [req("c2", page, method="GET", headers=notes)],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app, f"summarize {page}")

    assert result.outcome.status == "awaiting_approval"
    assert [seen.headers.get("x-notes") for seen in site.seen] == [None]  # only the page was read
    [approval] = app.approvals.pending()
    assert "THE-OWNERS-PRIVATE-NOTES" in approval.summary  # the owner sees what would leave
    row = audit_for(app, "http_request")[-1]
    assert (row.category, row.verdict, row.layer, row.tainted) == (
        "net_write",
        "ask",
        "taint",
        True,
    )


async def test_a_body_on_a_read_method_is_a_way_out_too(make_app, site):
    page = f"{site.base}/injection"
    model = ScriptedModel(
        [
            [fetch("c1", page)],
            [req("c2", page, method="OPTIONS", body="THE-OWNERS-PRIVATE-NOTES")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app, f"summarize {page}")
    assert result.outcome.status == "awaiting_approval"
    assert site.paths() == ["/injection"]


class _Run:
    """What `_category` asks of a run."""

    def __init__(self, *, tainted: bool, known: bool = True) -> None:
        self.tainted, self._known = tainted, known

    def knows_url(self, _url: str) -> bool:
        return self._known


@pytest.mark.parametrize(
    ("args", "tainted", "known", "category"),
    [
        ({"url": "https://a.example/x"}, True, True, Category.READ),
        ({"url": "https://a.example/x", "headers": []}, True, True, Category.READ),
        (
            {"url": "https://a.example/x", "headers": [{"name": "X", "value": "notes"}]},
            True,
            True,
            Category.NET_WRITE,
        ),
        (
            {"url": "https://a.example/x", "headers": [{"name": "Accept", "value": "text/html"}]},
            True,
            True,
            Category.NET_WRITE,
        ),
        (
            {"url": "https://a.example/x", "method": "OPTIONS", "body": "notes"},
            True,
            True,
            Category.NET_WRITE,
        ),
        # a secret placeholder is not the model's to fill: the broker only sends it to listed hosts
        (
            {
                "url": "https://a.example/x",
                "headers": [{"name": "Authorization", "value": "Bearer {{secret:gh}}"}],
            },
            True,
            True,
            Category.READ,
        ),
        (
            {
                "url": "https://a.example/x",
                "headers": [
                    {"name": "X", "value": "notes"},
                    {"name": "Authorization", "value": "{{secret:gh}}"},
                ],
            },
            True,
            True,
            Category.NET_WRITE,
        ),
        # an address not seen is a way out whatever else it carries; nothing is a way out before reading
        ({"url": "https://a.example/x"}, True, False, Category.NET_WRITE),
        (
            {"url": "https://a.example/x", "headers": [{"name": "X", "value": "notes"}]},
            False,
            True,
            Category.READ,
        ),
        ({"url": "https://a.example/x", "method": "POST"}, False, True, Category.NET_WRITE),
    ],
)
def test_what_a_request_carries_counts_once_the_run_has_read_outside_content(
    args, tainted, known, category
):
    from personal_agent.tools.web import _category

    assert _category(_Run(tainted=tainted, known=known), args) is category


async def test_the_same_host_is_no_excuse_the_whole_address_has_to_be_one_the_run_has_seen(
    make_app, site
):
    """The page that carries an injection lives on a host; asking only about *hosts* would let it
    name its own collection URL."""
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", f"{site.base}/text?x=notes")],
            [assistant_message("x")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval"


async def test_links_shown_on_a_page_can_be_followed_without_asking(make_app, site):
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/page")],
            [fetch("c2", f"{site.base}/docs")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed" and site.paths() == ["/page", "/docs"]
    assert app.approvals.pending() == []


async def test_links_cut_off_by_the_length_limit_are_not_trusted(make_app, site):
    """Only addresses the model was actually shown count."""
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/page", max_chars=200)],
            [fetch("c2", f"{site.base}/hidden-link")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval"


async def test_addresses_the_owner_wrote_need_no_approval_even_after_reading_a_page(make_app, site):
    asked = f"Read {site.base}/injection, then summarize ({site.base}/text)."
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", f"{site.base}/text")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app, asked)
    assert result.outcome.status == "completed" and site.paths() == ["/injection", "/text"]


async def test_addresses_in_a_timer_the_owner_set_need_no_approval(make_app, site):
    from helpers import timer

    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", f"{site.base}/text")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    app.inbox.add(timer("daily", f"Check {site.base}/injection and {site.base}/text every morning"))
    [result] = await drain(app)
    assert result.outcome.status == "silent"  # a scheduled run with nothing to report
    assert site.paths() == ["/injection", "/text"] and app.approvals.pending() == []


async def test_the_address_after_a_redirect_counts_as_seen(make_app, site):
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/redirect?to=/page")],
            [fetch("c2", f"{site.base}/page")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed"


async def test_a_fresh_run_may_look_anything_up_because_it_has_read_nothing_yet(make_app, site):
    model = ScriptedModel(
        [[fetch("c1", f"{site.base}/text?q=anything")], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed" and app.approvals.pending() == []


async def test_addresses_spelled_differently_but_pointing_to_the_same_place_match(make_app, site):
    port = site.port
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/page")],
            [fetch("c2", f"HTTP://127.0.0.1:{port}/docs#section")],  # case and #fragment differ
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed"


async def test_an_allow_when_tainted_rule_lets_a_trusted_api_through(make_app, site):
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", f"{site.base}/text?x=1")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg(allow_when_tainted=["web_fetch:http://127.0.0.1:*/text*"]))
    [result] = await run(app)
    assert result.outcome.status == "completed" and len(site.seen) == 2


async def test_read_only_mode_refuses_instead_of_asking(make_app, site):
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/injection")],
            [fetch("c2", f"{site.base}/text?x=1")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg(mode="read_only"))
    [result] = await run(app)
    assert result.outcome.status == "completed" and site.paths() == ["/injection"]
    assert outputs(app)["c2"].startswith("Denied:") and "read-only" in outputs(app)["c2"]
    assert app.approvals.pending() == []


async def test_in_read_only_mode_a_post_is_refused_but_a_plain_get_is_fine(make_app, site):
    model = ScriptedModel(
        [
            [req("c1", f"{site.base}/text")],
            [req("c2", f"{site.base}/echo", method="POST", body="x")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg(mode="read_only"))
    await run(app)
    assert "plain words" in outputs(app)["c1"] and outputs(app)["c2"].startswith("Denied:")
    assert site.paths() == ["/text"]


async def test_the_known_addresses_survive_a_wait_for_approval(make_app, site):
    """A run that waits for the owner resumes in a new process; what it had seen must come along."""
    model = ScriptedModel(
        [
            [fetch("c1", f"{site.base}/page")],  # shows a link to /docs
            [req("c2", f"{site.base}/echo", method="POST", body="x")],  # needs the owner
            [fetch("c3", f"{site.base}/docs")],  # a link from before the wait
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=cfg())
    [first] = await run(app)
    assert first.outcome.status == "awaiting_approval"

    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")
    [second] = await drain(app)
    assert second.outcome.status == "completed" and app.approvals.pending() == []
    assert site.paths() == ["/page", "/echo", "/docs"]


# ---- credentials ------------------------------------------------------------------------------------------------
@pytest.fixture
def names(monkeypatch):
    """`api.test` and `other.test` are two different sites that both live on this machine."""
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *a, **k):
        if host in ("api.test", "other.test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def secret_cfg(**policy) -> Config:
    return cfg(
        secrets={"gh": SecretConfig(env="GH_TOKEN", hosts=["api.test"])},
        allow_private=("127.0.0.1", "api.test", "other.test"),
        **policy,
    )


AUTH = [{"name": "Authorization", "value": "Bearer {{secret:gh}}"}]


def whole_record(app) -> str:
    """Everything the agent keeps: the conversation, the audit trail, the approvals."""
    return json.dumps(
        {
            "session": app.sessions.items("main"),
            "audit": [vars(r) for r in app.audit.recent(500)],
            "approvals": [vars(a) for a in app.approvals.pending()],
            "outbox": [vars(o) for o in app.outbox.recent()],
        },
        default=str,
    )


async def test_a_secret_reaches_the_server_it_is_meant_for_and_nowhere_else(
    make_app, site, names, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [[req("c1", f"http://api.test:{site.port}/echo", headers=AUTH)], [assistant_message("ok")]]
    )
    app = make_app(model, config=secret_cfg())
    [result] = await run(app)

    assert result.outcome.status == "completed"
    [seen] = site.seen
    assert seen.headers["authorization"] == f"Bearer {TOKEN}"
    assert seen.headers["host"] == f"api.test:{site.port}"
    # the server echoed the header back; the model got it scrubbed, and no record has the value
    assert TOKEN not in outputs(app)["c1"] and "[REDACTED" in outputs(app)["c1"]
    assert TOKEN not in whole_record(app)
    assert "{{secret:gh}}" in whole_record(app)  # what the model wrote is what was kept


async def test_secrets_work_in_the_url_and_the_body_too(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [
            [
                req("c1", f"http://api.test:{site.port}/echo?key={{{{secret:gh}}}}"),
                req(
                    "c2",
                    f"http://api.test:{site.port}/echo",
                    method="POST",
                    body='{"t": "{{secret:gh}}"}',
                ),
            ],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=secret_cfg(allow=["http_request"]))
    await run(app)
    by_path = {s.method: s for s in site.seen}
    assert by_path["GET"].path == f"/echo?key={TOKEN}"
    assert by_path["POST"].body == f'{{"t": "{TOKEN}"}}'.encode()
    assert TOKEN not in whole_record(app)
    # the model is shown the address it wrote, with the placeholder rather than the value
    assert outputs(app)["c1"].splitlines()[0].endswith("/echo?key={{secret:gh}}")


async def test_the_approval_question_never_contains_the_secret(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [
            [
                req(
                    "c1",
                    f"http://api.test:{site.port}/echo?k={{{{secret:gh}}}}",
                    method="POST",
                    headers=AUTH,
                    body="x",
                )
            ],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=secret_cfg())
    await run(app)
    [approval] = app.approvals.pending()
    assert TOKEN not in approval.summary and "{{secret:gh}}" in approval.summary
    assert TOKEN not in whole_record(app)
    app.approvals.decide(approval.id, True, by="owner")
    await drain(app)
    assert site.seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in whole_record(app)


async def test_a_secret_is_refused_for_any_other_site(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [
            [req("c1", f"http://other.test:{site.port}/echo", headers=AUTH)],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=secret_cfg())
    await run(app)
    out = outputs(app)["c1"]
    assert out.startswith("Error:") and "only be sent to api.test, not to other.test" in out
    assert site.seen == []  # nothing was sent anywhere


async def test_a_secret_cannot_be_smuggled_into_the_host_name(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [[req("c1", f"http://{{{{secret:gh}}}}.api.test:{site.port}/")], [assistant_message("ok")]]
    )
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert "cannot be part of the host name" in outputs(app)["c1"] and site.seen == []


async def test_a_secret_is_not_sent_over_plain_http_to_a_site_that_is_not_private(
    make_app, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    secrets = {"gh": SecretConfig(env="GH_TOKEN", hosts=["api.example.com"])}
    model = ScriptedModel(
        [[req("c1", "http://api.example.com/v1", headers=AUTH)], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg(secrets=secrets))
    await run(app)
    assert "only sent over https" in outputs(app)["c1"]


@pytest.mark.parametrize(
    ("header", "why"),
    [
        ("Bearer {{secret:nope}}", "no secret named 'nope'"),
        ("Bearer {{secret:bad name}}", "not a valid secret placeholder"),
        ("Bearer {{secret:}}", "not a valid secret placeholder"),
    ],
)
async def test_mistakes_with_placeholders_are_explained_not_sent(
    make_app, site, names, monkeypatch, header, why
):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    call = req(
        "c1",
        f"http://api.test:{site.port}/echo",
        headers=[{"name": "Authorization", "value": header}],
    )
    model = ScriptedModel([[call], [assistant_message("ok")]])
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert why in outputs(app)["c1"] and site.seen == []


async def test_a_secret_the_owner_has_not_set_yet_says_what_to_do(
    make_app, site, names, monkeypatch
):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    model = ScriptedModel(
        [[req("c1", f"http://api.test:{site.port}/echo", headers=AUTH)], [assistant_message("ok")]]
    )
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert "owner has to set GH_TOKEN" in outputs(app)["c1"] and site.seen == []


async def test_placeholders_in_header_names_are_refused(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    call = req(
        "c1",
        f"http://api.test:{site.port}/echo",
        headers=[{"name": "X-{{secret:gh}}", "value": "1"}],
    )
    model = ScriptedModel([[call], [assistant_message("ok")]])
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert outputs(app)["c1"].startswith("Error:") and site.seen == []


async def test_a_redirect_to_another_site_is_not_followed_when_the_request_carries_a_secret(
    make_app, site, names, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    hop = f"http://api.test:{site.port}/redirect?to=http://other.test:{site.port}/echo"
    model = ScriptedModel([[req("c1", hop, headers=AUTH)], [assistant_message("ok")]])
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert "redirected to another address" in outputs(app)["c1"]
    assert [s.path.split("?")[0] for s in site.seen] == [
        "/redirect"
    ]  # the second site was never asked


async def test_a_redirect_within_the_same_site_keeps_the_secret(make_app, site, names, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    hop = f"http://api.test:{site.port}/redirect?to=/echo"
    model = ScriptedModel([[req("c1", hop, headers=AUTH)], [assistant_message("ok")]])
    app = make_app(model, config=secret_cfg())
    await run(app)
    assert [s.path.split("?")[0] for s in site.seen] == ["/redirect", "/echo"]
    assert site.seen[1].headers["authorization"] == f"Bearer {TOKEN}"


async def test_a_secret_value_in_a_page_is_scrubbed_from_what_the_model_sees(
    make_app, site, names, monkeypatch
):
    """Even a value the agent never typed (the server repeats it) does not come back as text."""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    model = ScriptedModel(
        [
            [req("c1", f"http://api.test:{site.port}/echo?note=token-is-{TOKEN}")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=secret_cfg(allow=["http_request"]))
    await run(app)
    assert TOKEN not in outputs(app)["c1"]


# ---- hosted search ----------------------------------------------------------------------------------------------
async def test_a_hosted_search_is_audited_charged_and_counts_as_outside_content(make_app, site):
    model = ScriptedModel(
        [
            {
                "output": [
                    search("python release notes", f"{site.base}/text"),
                    assistant_message("found it"),
                ],
                "usage": usage(1000, 50),
            },
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)

    [row] = audit_for(app, "web_search")
    assert (row.category, row.verdict, row.layer, row.outcome, row.tainted) == (
        "read", "allow", "hosted", "ok", True,
    )  # fmt: skip
    assert row.detail == "search: python release notes"
    record = app.runs.get(result.outcome.run_id)
    assert record.tainted and record.tool_calls == 1
    from personal_agent.harness.pricing import cost_usd

    expected = (
        cost_usd("gpt-6.1-sol", input_tokens=1000, output_tokens=50)
        + app.config.tools.web_search_call_usd
    )
    assert record.cost_usd == pytest.approx(expected)


async def test_each_hosted_search_in_a_response_is_counted(make_app):
    model = ScriptedModel(
        [[search("a", call_id="ws_1"), search("b", call_id="ws_2"), assistant_message("done")]]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert len(audit_for(app, "web_search")) == 2
    assert app.runs.get(result.outcome.run_id).cost_usd >= 2 * app.config.tools.web_search_call_usd


async def test_opening_a_page_is_audited_with_its_address(make_app, site):
    item = ResponseFunctionWebSearch(
        id="ws_9", status="completed", type="web_search_call",
        action=ActionOpenPage(type="open_page", url=f"{site.base}/page"),
    )  # fmt: skip
    app = make_app(ScriptedModel([[item, assistant_message("read it")]]), config=cfg())
    await run(app)
    [row] = audit_for(app, "web_search")
    assert row.detail == f"open_page: {site.base}/page"


async def test_an_address_a_search_returned_can_be_fetched_without_asking(make_app, site):
    model = ScriptedModel(
        [
            [search("docs", f"{site.base}/page"), fetch("c1", f"{site.base}/page")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed" and site.paths() == ["/page"]


async def test_an_address_a_search_did_not_return_still_needs_approval(make_app, site):
    model = ScriptedModel(
        [
            [search("docs", f"{site.base}/page"), fetch("c1", f"{site.base}/collect?data=notes")],
            [assistant_message("ok")],
        ]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "awaiting_approval" and site.seen == []


async def test_cited_pages_in_the_answer_count_as_seen(make_app, site):
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText
    from openai.types.responses.response_output_text import AnnotationURLCitation

    cited = ResponseOutputMessage(
        id="m1", type="message", role="assistant", status="completed",
        content=[
            ResponseOutputText(
                type="output_text", text="See the docs.", logprobs=[],
                annotations=[AnnotationURLCitation(type="url_citation", url=f"{site.base}/docs", title="Docs", start_index=0, end_index=3)],
            )
        ],
    )  # fmt: skip
    model = ScriptedModel(
        [[search("docs"), cited, fetch("c1", f"{site.base}/docs")], [assistant_message("ok")]]
    )
    app = make_app(model, config=cfg())
    [result] = await run(app)
    assert result.outcome.status == "completed" and site.paths() == ["/docs"]


async def test_responses_without_searches_cost_nothing_extra_and_leave_the_run_clean(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=cfg())
    [result] = await run(app)
    record = app.runs.get(result.outcome.run_id)
    assert not record.tainted and record.tool_calls == 0 and audit_for(app, "web_search") == []
