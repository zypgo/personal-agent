"""The web page's routes: signing in, the conversation, pictures, approvals."""

from __future__ import annotations

import pytest

from personal_agent.channels.ingress import MAX_IMAGES, MAX_TEXT_CHARS
from personal_agent.channels.transcribe import TranscriptionError
from personal_agent.channels.web.api import PAIR_ATTEMPTS, Window, load_assets, sse
from personal_agent.config import AgentConfig, Config, MediaConfig
from personal_agent.core.approvals import PendingCall
from test_ingress import FakeTranscriber
from web_fake import JPEG, ORIGIN, PNG, Web


@pytest.fixture
def web(make_app) -> Web:
    site = Web(make_app(transcriber=FakeTranscriber()))
    site.pair()
    return site


@pytest.fixture
def stranger(make_app) -> Web:
    return Web(make_app(transcriber=FakeTranscriber()))


# ---- the door ----------------------------------------------------------------------------------
GET_ROUTES = [
    "/api/session",
    "/api/history",
    "/api/approvals",
    "/api/status",
    "/api/media/" + "0" * 64,
]
POST_ROUTES = ["/api/logout", "/api/messages", "/api/approvals/1"]


@pytest.mark.parametrize("path", GET_ROUTES)
def test_a_stranger_gets_nothing_from_any_route(stranger, path):
    response = stranger.client.get(path)
    assert response.status_code == 401 and response.json() == {"error": "Not signed in."}


@pytest.mark.parametrize("path", POST_ROUTES)
def test_a_stranger_cannot_change_anything(stranger, path):
    response = stranger.client.post(path, json={"decision": "approve"}, headers=stranger.write)
    assert response.status_code == 401


def test_a_stranger_cannot_open_the_event_stream(stranger):
    assert stranger.client.get("/api/stream").status_code == 401


def test_a_strangers_upload_is_never_parsed(stranger):
    # A malformed multipart body would be a 400 if anything looked at it; the answer is 401.
    response = stranger.client.post(
        "/api/messages",
        content=b"--x\r\ngarbage",
        headers={**stranger.write, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 401


def test_a_forged_cookie_is_nobody(stranger):
    stranger.client.cookies.set("pa_session", "A" * 43)
    assert stranger.client.get("/api/session").status_code == 401


# ---- pairing -----------------------------------------------------------------------------------
def test_pairing_sets_a_hardened_cookie_and_the_session_works(stranger):
    code = stranger.app.web_auth.invite("phone").code
    response = stranger.client.post("/api/pair", json={"code": code}, headers=stranger.write)
    assert response.status_code == 200 and response.json() == {"ok": True, "label": "phone"}
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("pa_session=")
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http on this machine would drop it
    assert code not in cookie  # the cookie is a different secret from the link
    assert stranger.client.get("/api/session").status_code == 200


def test_the_cookie_is_secure_when_the_public_address_is_https(make_app):
    site = Web(make_app(), public_url="https://mac.tail1234.ts.net")
    code = site.app.web_auth.invite().code
    response = site.client.post("/api/pair", json={"code": code}, headers=site.write)
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_the_cookie_lives_as_long_as_the_session(make_app):
    site = Web(make_app(config=Config(web={"session_days": 7})))
    response = site.pair()
    assert "max-age=604800" in response.headers["set-cookie"].lower()


def test_a_link_works_once(stranger):
    code = stranger.app.web_auth.invite().code
    assert (
        stranger.client.post("/api/pair", json={"code": code}, headers=stranger.write).status_code
        == 200
    )
    second = stranger.client.post("/api/pair", json={"code": code}, headers=stranger.write)
    assert second.status_code == 403 and "pa web pair" in second.json()["error"]


def test_a_wrong_code_is_refused_without_a_cookie(stranger):
    response = stranger.client.post("/api/pair", json={"code": "nope"}, headers=stranger.write)
    assert response.status_code == 403 and "set-cookie" not in response.headers


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"code": 5}', b"{}", b"null"])
def test_a_malformed_pairing_request_is_a_400(stranger, body):
    response = stranger.client.post(
        "/api/pair", content=body, headers={**stranger.write, "Content-Type": "application/json"}
    )
    assert response.status_code == 400


def test_guessing_is_slowed_for_everyone(stranger):
    count, _ = PAIR_ATTEMPTS
    for _ in range(count):
        assert (
            stranger.client.post(
                "/api/pair", json={"code": "x"}, headers=stranger.write
            ).status_code
            == 403
        )
    blocked = stranger.client.post("/api/pair", json={"code": "x"}, headers=stranger.write)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    # Even the right code waits its turn: the limit is on attempts, not on being wrong.
    code = stranger.app.web_auth.invite().code
    assert (
        stranger.client.post("/api/pair", json={"code": code}, headers=stranger.write).status_code
        == 429
    )
    stranger.app.clock.advance(61)
    assert (
        stranger.client.post("/api/pair", json={"code": code}, headers=stranger.write).status_code
        == 200
    )


def test_the_attempt_window_forgets(clock=None):
    window = Window(2, 10.0)
    assert window.hit(0.0) is None and window.hit(1.0) is None
    assert window.hit(2.0) == pytest.approx(8.0)
    assert window.hit(10.5) is None


def test_signing_out_ends_that_session_and_clears_the_cookie(web):
    response = web.client.post("/api/logout", headers=web.write)
    assert response.status_code == 200
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert web.client.get("/api/session").status_code == 401
    assert web.app.web_auth.sessions() == []


def test_ending_a_session_from_the_computer_signs_the_browser_out_at_once(web):
    [session] = web.app.web_auth.sessions()
    web.app.web_auth.revoke(session.id)
    assert web.client.get("/api/session").status_code == 401


def test_the_session_says_who_and_what_it_can_do(make_app):
    app = make_app(
        config=Config(
            agent=AgentConfig(name="Ada", owner_name="Frank"), media=MediaConfig(voice=False)
        )
    )
    site = Web(app)
    site.pair("my phone")
    info = site.client.get("/api/session").json()
    assert (info["agent"], info["owner"], info["label"]) == ("Ada", "Frank", "my phone")
    assert info["features"]["voice"] is False and info["features"]["images"] is True
    assert info["features"]["max_images"] == MAX_IMAGES
    assert info["last_id"] == 0 and len(info["version"]) == 12


# ---- history -----------------------------------------------------------------------------------
def fill(app, n: int) -> None:
    for i in range(n):
        app.transcript.add_in("web", f"message {i}")


def test_the_history_is_oldest_first_and_pages_backwards(web):
    fill(web.app, 25)
    page = web.client.get("/api/history", params={"limit": 10}).json()
    assert [t["text"] for t in page["turns"]] == [f"message {i}" for i in range(15, 25)]
    assert page["more"] is True
    oldest = page["turns"][0]["id"]
    older = web.client.get("/api/history", params={"limit": 10, "before": oldest}).json()
    assert [t["text"] for t in older["turns"]] == [f"message {i}" for i in range(5, 15)]
    last = web.client.get(
        "/api/history", params={"limit": 10, "before": older["turns"][0]["id"]}
    ).json()
    assert len(last["turns"]) == 5 and last["more"] is False


def test_an_empty_history_is_empty(web):
    assert web.client.get("/api/history").json() == {"turns": [], "more": False}


@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (1000, 100)])
def test_the_page_size_is_kept_in_bounds(web, limit, expected):
    fill(web.app, 120)
    assert len(web.client.get("/api/history", params={"limit": limit}).json()["turns"]) == expected


@pytest.mark.parametrize("params", [{"limit": "x"}, {"before": "x"}, {"before": "1.5"}])
def test_nonsense_paging_is_a_400(web, params):
    assert web.client.get("/api/history", params=params).status_code == 400


def test_a_turn_says_what_it_is(web):
    web.app.transcript.add_in("telegram", "from the phone")
    [turn] = web.client.get("/api/history").json()["turns"]
    assert {k: turn[k] for k in ("direction", "channel", "kind", "text")} == {
        "direction": "in",
        "channel": "telegram",
        "kind": "message",
        "text": "from the phone",
    }
    assert isinstance(turn["id"], int) and isinstance(turn["at"], float)


def test_an_approval_request_in_the_history_shows_what_became_of_the_request(web):
    app = web.app
    [approval] = app.approvals.create(
        "r1", "main", [PendingCall("c1", "shell", "git push origin main")]
    )
    app.outbox.enqueue(
        session_key="main",
        channel="web",
        text="May I push?",
        kind="approval_request",
        extra={"approval_ids": [approval.id]},
    )
    message = app.outbox.claim_due(channels={"web"})[0]
    app.outbox.mark_sent(message.id)
    app.transcript.add_out(message)
    [turn] = web.client.get("/api/history").json()["turns"]
    assert turn["kind"] == "approval_request"
    [shown] = turn["approvals"]
    assert (shown["id"], shown["tool"], shown["status"]) == (approval.id, "shell", "pending")
    assert shown["summary"] == "git push origin main"
    app.approvals.decide(approval.id, True, by="test")
    [again] = web.client.get("/api/history").json()["turns"][0]["approvals"]
    assert again["status"] == "approved"  # read when asked, not frozen when the message was sent


# ---- sending -----------------------------------------------------------------------------------
def test_a_message_is_queued_for_the_agent_and_comes_back_as_a_turn(web):
    response = web.send("what is on today?", client_id="client-0001")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued" and body["turn"]["text"] == "what is on today?"
    [event] = web.events()
    assert (event["source"], event["kind"], event["trust"]) == ("web", "message", "owner")
    assert event["external_id"] == "web:client-0001"
    assert event["payload"] == {"text": "what is on today?", "recipient": None}
    assert web.woken == 1


def test_sending_the_same_message_twice_queues_it_once(web):
    first = web.send("pay the invoice", client_id="client-0002")
    second = web.send("pay the invoice", client_id="client-0002")
    assert (first.json()["status"], second.json()["status"]) == ("queued", "duplicate")
    assert len(web.events()) == 1 and len(web.app.transcript.latest()) == 1


def test_a_message_without_a_usable_id_still_goes_through_but_cannot_be_deduplicated(web):
    web.send("one", client_id="x")  # too short to be an id
    web.send("one")
    assert len(web.events()) == 2


def test_a_picture_comes_along(web):
    response = web.send("what is this?", images=[(PNG, "image/png"), (JPEG, "image/jpeg")])
    assert response.status_code == 200
    [event] = web.events()
    kinds = [a["kind"] for a in event["payload"]["attachments"]]
    assert kinds == ["image", "image"]
    media_id = event["payload"]["attachments"][0]["id"]
    assert web.app.media.read(media_id) is not None


@pytest.mark.parametrize(
    ("images", "voice", "status"),
    [
        ([(PNG, "image/png"), (JPEG, "image/jpeg")], None, 200),
        ([(b"<html>not an image</html>", "image/png")], None, 422),  # refused after being read
        ([(PNG, "image/png")] * (MAX_IMAGES + 1), None, 422),
        ([], (b"\x00" * 2_000, "audio/webm"), 200),
    ],
)
def test_every_uploaded_file_is_closed_whatever_became_of_the_message(
    web, monkeypatch, images, voice, status
):
    from starlette.datastructures import UploadFile

    opened: list[UploadFile] = []
    original = UploadFile.__init__

    def counting(self, *args, **kwargs):
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(UploadFile, "__init__", counting)

    response = web.send("here", images=images, voice=voice, voice_seconds=3.0 if voice else None)

    assert response.status_code == status
    assert opened, "the request carried no files"
    assert all(upload.file.closed for upload in opened), "an uploaded file was left open"


def test_a_file_that_is_not_a_picture_is_refused_whatever_it_claims_to_be(web):
    response = web.send("look", images=[(b"<html>not an image</html>", "image/png")])
    assert response.status_code == 422 and "not a picture" in response.json()["error"]
    assert web.events() == []


def test_too_many_pictures_are_refused(web):
    response = web.send("lots", images=[(PNG, "image/png")] * (MAX_IMAGES + 1))
    assert response.status_code == 422 and str(MAX_IMAGES) in response.json()["error"]


def test_a_voice_message_is_turned_into_words(web):
    response = web.send(voice=(b"\x00" * 2_000, "audio/webm"), voice_seconds=3.2)
    assert response.status_code == 200
    [event] = web.events()
    assert event["payload"]["text"] == "remind me to call mom" and event["payload"]["voice"] is True
    transcriber: FakeTranscriber = web.app.transcriber
    assert transcriber.calls == [(2_000, "audio/webm", "note.webm")]


def test_a_voice_message_that_cannot_be_understood_says_why(make_app):
    site = Web(
        make_app(transcriber=FakeTranscriber(error=TranscriptionError("the service is down")))
    )
    site.pair()
    response = site.send(voice=(b"\x00" * 2_000, "audio/webm"))
    assert response.status_code == 422 and "the service is down" in response.json()["error"]


def test_a_command_is_answered_without_the_agent(web):
    response = web.send("/status", client_id="client-0003")
    assert response.status_code == 200 and response.json()["status"] == "command"
    assert web.events() == []
    [reply] = web.app.outbox.recent()
    assert reply.channel == "web" and "Idle." in reply.text and reply.payload["interactive"] is True


def test_an_empty_message_is_refused(web):
    response = web.send("   ")
    assert response.status_code == 422 and "empty" in response.json()["error"]


def test_a_huge_message_is_refused(web):
    response = web.send("x" * (MAX_TEXT_CHARS + 1))
    assert response.status_code == 422 and f"{MAX_TEXT_CHARS:,}" in response.json()["error"]


def test_a_message_must_be_a_form(web):
    response = web.client.post("/api/messages", json={"text": "hi"}, headers=web.write)
    assert response.status_code == 415


def test_a_message_sent_from_the_page_is_answered_to_the_page(web):
    web.send("hello", client_id="client-0004")
    [event] = web.events()
    assert event["payload"]["recipient"] is None and event["source"] == "web"


# ---- pictures ----------------------------------------------------------------------------------
def stored(web, data=PNG) -> str:
    return web.app.media.put(data).id


def test_a_picture_is_served_with_its_real_type_and_cannot_run_anything(web):
    media_id = stored(web)
    response = web.client.get(f"/api/media/{media_id}")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        "immutable" in response.headers["cache-control"]
        and "private" in response.headers["cache-control"]
    )


@pytest.mark.parametrize(
    "media_id",
    [
        "../../../etc/passwd",
        "..%2f..%2fsecret",
        "0" * 64,
        "G" * 64,
        "a" * 63,
        "a" * 65,
        ".." + "a" * 62,
    ],
)
def test_nothing_but_a_stored_picture_is_ever_served(web, media_id):
    stored(web)
    assert web.client.get(f"/api/media/{media_id}").status_code == 404


def test_a_file_dropped_next_to_the_pictures_is_not_reachable(web):
    web.app.media.directory.mkdir(parents=True, exist_ok=True)
    (web.app.media.directory / "notes.txt").write_text("secret")
    assert web.client.get("/api/media/notes.txt").status_code == 404
    assert web.client.get("/api/media/notes").status_code == 404


# ---- approvals ---------------------------------------------------------------------------------
def ask(web, summary="git push origin main", tool="shell"):
    [approval] = web.app.approvals.create("r1", "main", [PendingCall("c1", tool, summary)])
    return approval


def test_waiting_approvals_are_listed(web):
    approval = ask(web)
    [shown] = web.client.get("/api/approvals").json()["pending"]
    assert (shown["id"], shown["tool"], shown["status"]) == (approval.id, "shell", "pending")


def test_a_long_summary_is_cut(web):
    ask(web, summary="x" * 5000)
    [shown] = web.client.get("/api/approvals").json()["pending"]
    assert len(shown["summary"]) == 800


def test_approving_lets_the_paused_work_continue(web):
    approval = ask(web)
    response = web.client.post(
        f"/api/approvals/{approval.id}", json={"decision": "approve"}, headers=web.write
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["approval"]["status"] == "approved"
    assert web.app.approvals.get(approval.id).decided_by == "owner:web"
    assert web.client.get("/api/approvals").json()["pending"] == []


def test_denying_can_say_why(web):
    approval = ask(web)
    response = web.client.post(
        f"/api/approvals/{approval.id}",
        json={"decision": "deny", "note": "not on a friday"},
        headers=web.write,
    )
    assert response.json()["approval"]["status"] == "denied"
    assert web.app.approvals.get(approval.id).note == "not on a friday"


def test_a_decision_wakes_the_agent_so_the_work_resumes(web):
    approval = ask(web)
    web.app.approvals.save_run("r1", "main", "{}", {})  # the paused run the answer will resume
    before = web.woken
    web.client.post(
        f"/api/approvals/{approval.id}", json={"decision": "approve"}, headers=web.write
    )
    assert web.woken == before + 1
    [resume] = [e for e in web.events() if e["kind"] == "approval_response"]
    assert resume["payload"] == {"run_id": "r1"} and resume["trust"] == "owner"


def test_a_decision_with_nothing_to_resume_does_not_wake_anyone(web):
    approval = ask(web)
    before = web.woken
    web.client.post(
        f"/api/approvals/{approval.id}", json={"decision": "approve"}, headers=web.write
    )
    assert web.woken == before and web.events() == []


@pytest.mark.parametrize("body", [{"decision": "maybe"}, {}, {"decision": 1}, [], "approve"])
def test_a_decision_must_be_approve_or_deny(web, body):
    approval = ask(web)
    response = web.client.post(f"/api/approvals/{approval.id}", json=body, headers=web.write)
    assert response.status_code == 400
    assert web.app.approvals.get(approval.id).status == "pending"


def test_a_request_that_was_already_settled_says_so(web):
    approval = ask(web)
    url = f"/api/approvals/{approval.id}"
    web.client.post(url, json={"decision": "deny"}, headers=web.write)
    again = web.client.post(url, json={"decision": "approve"}, headers=web.write)
    assert again.json()["ok"] is False and "already denied" in again.json()["message"]
    assert web.app.approvals.get(approval.id).status == "denied"


def test_an_unknown_request_is_explained(web):
    response = web.client.post("/api/approvals/99", json={"decision": "approve"}, headers=web.write)
    assert response.json()["ok"] is False and "no approval #99" in response.json()["message"]


def test_a_request_id_that_is_not_a_number_is_a_400(web):
    response = web.client.post(
        "/api/approvals/abc", json={"decision": "approve"}, headers=web.write
    )
    assert response.status_code == 400


# ---- status ------------------------------------------------------------------------------------
def test_the_status_says_what_the_terminal_status_does(web):
    ask(web)
    status = web.client.get("/api/status").json()
    assert status["working"] is False and status["pending_approvals"] == 1
    assert status["daily_limit"] == 5.0 and status["spent_today"] == 0
    assert "Idle." in status["text"] and "waiting for your answer" in status["text"]


# ---- the page and its files --------------------------------------------------------------------
def test_the_page_and_its_files_are_public_and_carry_the_current_version(stranger):
    index = stranger.client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    version = stranger.client.get("/api/session")  # needs a session; the version is in the page
    assert version.status_code == 401
    assert "{{version}}" not in index.text
    worker = stranger.client.get("/sw.js")
    assert worker.headers["content-type"].startswith("text/javascript")
    assert "{{version}}" not in worker.text
    manifest = stranger.client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")


def test_files_are_revalidated_not_trusted_blindly(stranger):
    first = stranger.client.get("/static/app.js")
    assert first.headers["cache-control"] == "no-cache" and first.headers["etag"]
    again = stranger.client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    changed = stranger.client.get("/static/app.js", headers={"If-None-Match": '"other"'})
    assert changed.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/static/nope.js",
        "/static/..%2Fapi.py",
        "/static/../api.py",
        "/static/%2e%2e/secret",
        "/static/",
        "/static",
    ],
)
def test_only_the_pages_own_files_are_served(stranger, path):
    assert stranger.client.get(path).status_code in (404, 307)


def test_unknown_api_paths_are_an_error_not_the_page(stranger):
    assert stranger.client.get("/api/nope").status_code == 404


def test_the_set_of_files_is_fixed_when_the_server_starts(tmp_path):
    (tmp_path / "app.js").write_text("a")
    (tmp_path / "index.html").write_text("v={{version}}")
    (tmp_path / ".secret.js").write_text("hidden")
    (tmp_path / "notes.py").write_text("print()")
    (tmp_path / "sub").mkdir()
    assets, version = load_assets(tmp_path)
    assert set(assets) == {"app.js", "index.html"}
    assert assets["index.html"].body == f"v={version}".encode()
    (tmp_path / "app.js").write_text("b")
    assert load_assets(tmp_path)[1] != version  # any change is a new version


def test_the_page_never_has_inline_code_or_outside_addresses():
    assets, _ = load_assets()
    html = assets["index.html"].body.decode()
    assert "<script>" not in html and " onclick" not in html and "style=" not in html
    assert "http://" not in html and "https://" not in html


# ---- server-sent events ------------------------------------------------------------------------
def test_an_event_is_one_json_line():
    event = sse("turn", {"text": "line one\nline two", "n": 1}, 7).decode()
    assert event == 'id: 7\nevent: turn\ndata: {"text":"line one\\nline two","n":1}\n\n'
    assert sse("activity", {"working": True}).decode() == (
        'event: activity\ndata: {"working":true}\n\n'
    )


def test_the_page_is_told_only_what_it_asks_for():
    assert ORIGIN.startswith("http://127.0.0.1")  # keeps the import honest
