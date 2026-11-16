"""`pa web`: pairing browsers with the chat page, from the computer and nowhere else."""

from __future__ import annotations

import re
import webbrowser

import pytest

from helpers import invoke
from personal_agent.config import Config, WebConfig

PUBLIC = "https://my-mac.tailnet-name.ts.net"
LINK = re.compile(r"(https?://\S+)#pair=([A-Za-z0-9_-]+)")


def flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def ready(wired):
    def go(**web):
        return wired(config=Config(web=WebConfig(**web)))

    return go


def link_in(output: str) -> tuple[str, str]:
    """The address and the code of the pairing link a command printed."""
    found = LINK.search(output)  # the link is printed on one line, never wrapped
    assert found, output
    return found.group(1), found.group(2)


# ---- the overview -----------------------------------------------------------------------------
def test_the_overview_says_the_page_is_off_and_how_to_turn_it_on(ready):
    ready()
    out = flat(invoke("web").output)
    assert "off" in out and "enabled = true" in out


def test_the_overview_says_nothing_is_serving_without_the_service(ready):
    ready(enabled=True)
    out = flat(invoke("web").output)
    assert "not serving" in out and "pa serve" in out
    assert "http://127.0.0.1:8765" in out and "0 paired" in out
    assert "pa web pair" in out


def test_the_overview_counts_browsers_and_waiting_links(ready):
    app = ready(enabled=True)
    app.web_auth.redeem(app.web_auth.invite("laptop").code)
    app.web_auth.invite("phone")
    out = flat(invoke("web").output)
    assert "1 paired, 1 link(s) waiting" in out


def test_the_overview_explains_how_to_reach_the_page_from_a_phone(ready):
    ready(enabled=True)
    assert "pa web connect --via tailscale" in flat(invoke("web").output)


def test_with_a_public_address_the_overview_does_not_nag(ready):
    ready(enabled=True, public_url=PUBLIC)
    out = flat(invoke("web").output)
    assert PUBLIC in out and "pa web connect" not in out


# ---- pairing ----------------------------------------------------------------------------------
def test_pairing_needs_the_page_to_be_switched_on(ready):
    ready()
    result = invoke("web", "pair")
    assert result.exit_code == 1 and "switched off" in flat(result.output)


def test_pair_prints_a_link_that_works_once(ready):
    app = ready(enabled=True)
    result = invoke("web", "pair", "--label", "phone")
    assert result.exit_code == 0
    address, code = link_in(result.output)
    assert address == "http://127.0.0.1:8765/"
    assert "works once" in flat(result.output) and "10 minutes" in flat(result.output)

    first = app.web_auth.redeem(code)
    assert first is not None and first.session.label == "phone"
    assert app.web_auth.redeem(code) is None  # used up


def test_a_link_for_this_computer_is_not_offered_as_a_qr_code(ready):
    ready(enabled=True)
    out = invoke("web", "pair").output
    assert "only works on this computer" in flat(out)
    assert "█" not in out and "▀" not in out and "▄" not in out


def test_a_public_address_makes_the_link_and_a_qr_code_for_the_phone(ready):
    ready(enabled=True, public_url=PUBLIC)
    out = invoke("web", "pair").output
    address, _code = link_in(out)
    assert address == PUBLIC + "/"
    assert any(block in out for block in "█▀▄")  # the QR code
    assert "only works on this computer" not in flat(out)


def test_the_qr_code_can_be_left_out(ready):
    ready(enabled=True, public_url=PUBLIC)
    out = invoke("web", "pair", "--no-qr").output
    assert LINK.search(out) and not any(block in out for block in "█▀▄")


def test_the_qr_code_holds_the_whole_link(ready):
    """Whatever a phone scans must be the link that was printed, nothing cut off."""
    from personal_agent.commands.web import qr_text

    url = f"{PUBLIC}/#pair={'x' * 32}"
    art = qr_text(url)
    lines = art.splitlines()
    assert len(lines) >= 15 and len({len(line) for line in lines}) == 1  # a square, no ragged edge


def test_pair_warns_when_nothing_serves_the_page(ready):
    ready(enabled=True)
    assert "run `pa serve`" in flat(invoke("web", "pair").output)


def test_a_flood_of_links_is_stopped_and_can_be_cancelled(ready):
    app = ready(enabled=True)
    for _ in range(5):
        assert invoke("web", "pair").exit_code == 0
    refused = invoke("web", "pair")
    assert refused.exit_code == 1 and "still waiting" in flat(refused.output)

    assert "Cancelled 5 link(s)" in invoke("web", "unpair").output
    assert app.web_auth.pending_invites() == []
    assert invoke("web", "pair").exit_code == 0


def test_unpair_says_so_when_nothing_was_waiting(ready):
    ready(enabled=True)
    assert "No link was waiting" in invoke("web", "unpair").output


def test_a_label_that_looks_like_markup_is_shown_as_typed(ready):
    app = ready(enabled=True)
    app.web_auth.redeem(app.web_auth.invite("[red]phone[/red]").code)
    assert "[red]phone[/red]" in invoke("web", "sessions").output


# ---- opening the page here --------------------------------------------------------------------
def test_open_hands_the_browser_a_fresh_link(ready, monkeypatch):
    app = ready(enabled=True)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    result = invoke("web", "open")
    assert result.exit_code == 0 and "Opened the page" in result.output
    [url] = opened
    code = url.split("#pair=")[1]
    assert url.startswith("http://127.0.0.1:8765/#pair=")
    assert app.web_auth.redeem(code) is not None


def test_open_prints_the_link_when_there_is_no_browser(ready, monkeypatch):
    ready(enabled=True)
    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    out = invoke("web", "open").output
    assert "Could not start a browser" in out and "#pair=" in out


def test_open_needs_the_page_to_be_switched_on(ready, monkeypatch):
    ready()
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("opened a browser"))
    assert invoke("web", "open").exit_code == 1


# ---- the paired browsers ----------------------------------------------------------------------
def pair(app, label="phone", agent="Mozilla/5.0 (iPhone) Safari/605.1"):
    return app.web_auth.redeem(app.web_auth.invite(label).code, agent=agent).session


def test_sessions_lists_who_is_paired_and_how_to_pair(ready):
    app = ready(enabled=True)
    assert "No browser is paired" in invoke("web", "sessions").output
    pair(app)
    out = flat(invoke("web", "sessions").output)
    assert "phone" in out and "Safari on iPhone" in out and "active" in out


def test_revoking_signs_a_browser_out_at_once(ready):
    app = ready(enabled=True)
    session = pair(app)
    fresh = app.web_auth.redeem(app.web_auth.invite("other").code)
    result = invoke("web", "revoke", str(session.id))
    assert result.exit_code == 0 and "Signed out" in result.output
    assert app.web_auth.lookup(fresh.token) is not None  # the other one is untouched
    assert [s.label for s in app.web_auth.sessions()] == ["other"]
    assert "revoked" in invoke("web", "sessions", "--all").output


def test_revoking_something_that_is_not_there_fails(ready):
    ready(enabled=True)
    result = invoke("web", "revoke", "99")
    assert result.exit_code == 1 and "No such active session" in flat(result.output)


def test_revoke_all_signs_everybody_out(ready):
    app = ready(enabled=True)
    pair(app, "a")
    pair(app, "b")
    assert "Signed out 2 browser(s)" in invoke("web", "revoke", "--all").output
    assert app.web_auth.sessions() == []


@pytest.mark.parametrize("args", [(), ("1", "--all")])
def test_revoke_wants_one_thing_or_all(ready, args):
    app = ready(enabled=True)
    pair(app)
    assert invoke("web", "revoke", *args).exit_code == 2
    assert len(app.web_auth.sessions()) == 1  # nothing was ended by a malformed command


def test_the_pairing_code_is_never_stored_readably(ready):
    """What `pa web pair` prints is the only copy of the code; the database holds a hash."""
    app = ready(enabled=True)
    _address, code = link_in(invoke("web", "pair").output)
    dump = " ".join(str(tuple(row)) for row in app.db.query("SELECT * FROM web_invites"))
    assert code not in dump


LONG_PUBLIC = "https://a-rather-long-machine-name.a-rather-long-tailnet-name.ts.net:8443"


@pytest.mark.parametrize("command", ["pair", "open"])
def test_a_link_longer_than_the_terminal_is_never_broken_over_two_lines(
    ready, monkeypatch, command
):
    ready(enabled=True, public_url=LONG_PUBLIC)
    monkeypatch.setattr(webbrowser, "open", lambda url: False)  # `open` prints the link instead
    out = invoke("web", command, *(["--no-qr"] if command == "pair" else [])).output
    [line] = [ln for ln in out.splitlines() if "#pair=" in ln]
    assert len(line.strip()) > 80, "the test needs a link longer than the terminal is wide"
    address, code = link_in(out)
    assert address == LONG_PUBLIC + "/" and len(code) == 32  # all of it, on one line
