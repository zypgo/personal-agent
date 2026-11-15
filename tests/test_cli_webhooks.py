"""`pa webhooks`: what may call the agent, whether each route is ready, and a way to try one."""

from __future__ import annotations

import json

import httpx
import pytest

from helpers import invoke, webhook
from personal_agent.channels.webhooks import WebhookService, new_secret, secret_key
from personal_agent.channels.webhooks.service import REFUSED_KEY
from personal_agent.commands import webhooks as commands
from personal_agent.config import Config

ROUTES = {
    "github": {"scheme": "github", "secret_env": "GITHUB_WEBHOOK_SECRET"},
    "ci": {"scheme": "standard", "secret_env": "CI_WEBHOOK_SECRET", "events": ["build.failed"]},
    "cron": {"scheme": "generic", "secret_env": "CRON_WEBHOOK_SECRET"},
    "gl": {"scheme": "gitlab", "secret_env": "GITLAB_WEBHOOK_SECRET"},
}
ENV = {
    "GITHUB_WEBHOOK_SECRET": new_secret("github"),
    "CI_WEBHOOK_SECRET": new_secret("standard"),
    "CRON_WEBHOOK_SECRET": new_secret("generic"),
    "GITLAB_WEBHOOK_SECRET": new_secret("gitlab"),
}


def flat(output: str) -> str:
    return " ".join(output.split())


def queued(app) -> list[dict]:
    """Events waiting in the inbox, as plain dictionaries."""
    rows = app.db.query("SELECT source, kind, trust, payload FROM events WHERE status = 'pending'")
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


@pytest.fixture
def ready(wired, monkeypatch):
    for name in (*ENV, "OTHER"):
        monkeypatch.delenv(name, raising=False)

    def go(*, secrets: bool = True, routes: dict | None = None, **hooks):
        if secrets:
            for name, value in ENV.items():
                monkeypatch.setenv(name, value)
        settings = {"enabled": True, "routes": ROUTES if routes is None else routes, **hooks}
        return wired(config=Config.model_validate({"webhooks": settings}))

    return go


# ---- the overview -----------------------------------------------------------------------------
def test_the_overview_says_webhooks_are_off(wired):
    wired()
    out = flat(invoke("webhooks").output)
    assert "off" in out and "enabled = true" in out


def test_the_overview_asks_for_a_route_when_there_is_none(ready):
    ready(routes={})
    assert "No route is configured" in flat(invoke("webhooks").output)


def test_the_overview_lists_every_route_with_its_address_and_scheme(ready):
    ready()
    out = flat(invoke("webhooks").output)
    for name, route in ROUTES.items():
        assert name in out and route["scheme"] in out
        assert f"http://127.0.0.1:8766/hook/{name}" in out
    assert "build.failed" in out  # the route that only wants some events
    assert out.count("last delivery never") == len(ROUTES)  # nothing has been delivered yet
    assert "ready" in out and "not ready" not in out


def test_the_public_address_is_what_senders_are_given(ready):
    ready(public_url="https://hooks.example.com")
    out = flat(invoke("webhooks").output)
    assert "https://hooks.example.com/hook/github" in out
    assert "put a tunnel" not in out


def test_without_a_public_address_it_says_how_the_internet_gets_in(ready):
    ready()
    assert "put a tunnel" in flat(invoke("webhooks").output)


def test_a_route_without_its_secret_says_which_variable_to_set(ready):
    ready(secrets=False)
    out = flat(invoke("webhooks").output)
    assert "not ready" in out and "GITHUB_WEBHOOK_SECRET is not set" in out


def test_a_secret_that_would_not_protect_anything_is_reported(ready, monkeypatch):
    ready()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "weakpw")
    out = flat(invoke("webhooks").output)
    assert "GITHUB_WEBHOOK_SECRET" in out and "shorter than 16 characters" in out
    assert "weakpw" not in out  # not even the bad one is printed


def test_a_working_secret_is_never_printed(ready):
    ready()
    out = invoke("webhooks").output + invoke("webhooks", "send", "github").output
    assert not any(value in out for value in ENV.values())


def test_the_overview_shows_the_last_delivery_and_the_last_refusal(ready):
    app = ready()
    app.inbox.add(webhook("github", {"action": "opened"}))
    app.db.kv_set(
        f"{REFUSED_KEY}github",
        json.dumps({"at": app.clock.now() - 90, "why": "the signature does not match", "total": 3}),
        now=app.clock.now(),
    )
    out = flat(invoke("webhooks").output)
    assert "last delivery 0s ago (pending); 1 in the last day" in out
    assert "last refusal 1m ago: the signature does not match (3 in all)" in out


def test_only_the_last_day_is_counted_as_recent(ready):
    app = ready()
    app.inbox.add(webhook("github", {"n": 1}))
    app.clock.advance(2 * 86400)  # the first delivery is now two days old
    app.inbox.add(webhook("github", {"n": 2}))
    out = flat(invoke("webhooks").output)
    assert "last delivery 0s ago (pending); 1 in the last day" in out


# ---- making a secret --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["github", "ci", "cron", "gl"])
def test_a_new_secret_fits_the_scheme_and_says_where_it_goes(ready, name):
    app = ready(secrets=False)
    out = invoke("webhooks", "secret", name).output
    env_name = ROUTES[name]["secret_env"]
    line = next(line.strip() for line in out.splitlines() if line.strip().startswith(env_name))
    value = line.split("=", 1)[1]
    secret_key(ROUTES[name]["scheme"], value)  # the route would accept it
    assert str(app.paths.env_file) in out
    assert "Give the same secret to the sender" in flat(out)


def test_making_a_secret_writes_nothing(ready):
    app = ready(secrets=False)
    before = app.paths.env_file.read_bytes() if app.paths.env_file.exists() else None
    invoke("webhooks", "secret", "github")
    after = app.paths.env_file.read_bytes() if app.paths.env_file.exists() else None
    assert before == after


def test_a_standard_secret_has_the_prefix_its_senders_expect(ready):
    ready(secrets=False)
    assert "CI_WEBHOOK_SECRET=whsec_" in invoke("webhooks", "secret", "ci").output


def test_each_secret_is_new(ready):
    ready(secrets=False)
    first = invoke("webhooks", "secret", "github").output
    second = invoke("webhooks", "secret", "github").output
    assert first != second


def test_a_secret_can_be_made_before_the_route_exists(ready):
    ready(routes={})
    out = invoke("webhooks", "secret", "--scheme", "github").output
    assert "WEBHOOK_SECRET=" in out and "Secret field" in flat(out)


def test_an_unknown_route_or_scheme_is_refused(ready):
    ready()
    unknown = invoke("webhooks", "secret", "nope")
    assert unknown.exit_code == 1 and "No route called nope" in flat(unknown.output)
    assert "github" in unknown.output  # it lists the routes there are
    assert invoke("webhooks", "secret").exit_code == 2
    assert invoke("webhooks", "secret", "--scheme", "bogus").exit_code == 2


# ---- trying a route ---------------------------------------------------------------------------
@pytest.fixture
def listener(monkeypatch):
    """Stand in for the network: the delivery goes to the route's own checking code."""
    sent: list[dict] = []

    def attach(app, *, status_override: int | None = None):
        service = WebhookService(app, environ=ENV)
        monkeypatch.setattr(commands.time, "time", app.clock.now)  # sign with the app's own time

        def post(url, *, content, headers, timeout):
            name = url.rsplit("/", 1)[-1]
            sent.append({"url": url, "headers": dict(headers), "body": content})
            if status_override is not None:
                return httpx.Response(status_override, json={"error": "not found"})
            lowered = {k.lower(): v for k, v in headers.items()}
            reply = service.accept(service.routes[name], lowered, content, app.inbox.add)
            return httpx.Response(reply.status, json=reply.body)

        monkeypatch.setattr(commands.httpx, "post", post)
        return sent

    return attach


def test_a_github_ping_checks_the_secret_and_wakes_nobody(ready, listener):
    app = ready()
    sent = listener(app)
    result = invoke("webhooks", "send", "github")
    assert result.exit_code == 0 and "200" in result.output and "pong" in result.output
    assert sent[0]["url"] == "http://127.0.0.1:8766/hook/github"
    assert queued(app) == []


def test_a_real_event_asks_before_it_wakes_the_agent(ready, listener):
    app = ready()
    sent = listener(app)
    result = invoke("webhooks", "send", "github", "--event", "push", input="n\n")
    assert result.exit_code == 1 and "costs a few cents" in flat(result.output)
    assert sent == [] and app.inbox.counts().get("pending", 0) == 0


def test_a_confirmed_real_event_is_queued_as_outside_data(ready, listener):
    app = ready()
    listener(app)
    result = invoke("webhooks", "send", "github", "--event", "push", input="y\n")
    assert result.exit_code == 0 and "202" in result.output and "queued" in result.output
    [event] = queued(app)
    assert event["source"] == "webhook:github" and event["trust"] == "untrusted"
    assert event["payload"]["event"] == "push.test"  # the event and the sample's action


@pytest.mark.parametrize("name", ["github", "ci", "cron", "gl"])
def test_every_scheme_signs_what_its_route_checks(ready, listener, name):
    app = ready()
    listener(app)
    body = json.dumps({"type": "build.failed", "action": "x"})
    event = "push" if name == "github" else None
    args = ["webhooks", "send", name, "--yes", "--body", body] + (
        ["--event", event] if event else []
    )
    result = invoke(*args)
    assert result.exit_code == 0 and "202" in result.output, result.output
    assert len(queued(app)) == 1


def test_the_body_can_come_from_a_file(ready, listener, tmp_path):
    app = ready()
    sent = listener(app)
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"action": "from a file"}))
    invoke("webhooks", "send", "cron", "--yes", "--body", f"@{path}")
    assert json.loads(sent[0]["body"]) == {"action": "from a file"}
    assert queued(app)[0]["payload"]["body"] == {"action": "from a file"}


def test_a_route_whose_secret_is_missing_cannot_be_tried(ready, listener):
    app = ready(secrets=False)
    sent = listener(app)
    result = invoke("webhooks", "send", "github")
    assert result.exit_code == 1 and "cannot be used" in flat(result.output)
    assert sent == []


def test_an_unknown_route_cannot_be_tried(ready):
    ready()
    result = invoke("webhooks", "send", "nope")
    assert result.exit_code == 1 and "No route called nope" in result.output


def test_a_refusal_is_explained_without_giving_the_sender_more(ready, listener):
    app = ready()
    listener(app, status_override=404)
    result = invoke("webhooks", "send", "github")
    assert result.exit_code == 1 and "404" in result.output
    assert "what a refusal looks like" in flat(result.output)


def test_nothing_listening_is_reported_with_the_likely_reason(ready, monkeypatch):
    ready()

    def refuse(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(commands.httpx, "post", refuse)
    result = invoke("webhooks", "send", "github")
    assert result.exit_code == 1 and "Is `pa serve` running?" in flat(result.output)


def test_a_custom_address_can_be_tried(ready, listener):
    app = ready()
    sent = listener(app)
    invoke("webhooks", "send", "github", "--url", "http://127.0.0.1:9/hook/github")
    assert sent[0]["url"] == "http://127.0.0.1:9/hook/github"


@pytest.mark.parametrize(
    ("bound", "dialled"),
    [
        ("127.0.0.1", "http://127.0.0.1:8766/hook/github"),
        ("0.0.0.0", "http://127.0.0.1:8766/hook/github"),
        ("::", "http://127.0.0.1:8766/hook/github"),
        ("::1", "http://[::1]:8766/hook/github"),
        ("100.101.102.103", "http://100.101.102.103:8766/hook/github"),
    ],
)
def test_the_listener_is_dialled_where_it_can_be_reached(ready, listener, bound, dialled):
    app = ready(host=bound)
    sent = listener(app)
    invoke("webhooks", "send", "github")
    assert sent[0]["url"] == dialled
