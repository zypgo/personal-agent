from __future__ import annotations

import httpx
import pytest

from personal_agent.channels.telegram.api import (
    NotSent,
    TelegramApi,
    TelegramApiError,
    _client,
)
from telegram_fake import TOKEN, Fail, FakeTelegram


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def api(fake) -> TelegramApi:
    return TelegramApi(TOKEN, client=fake.client())


# ---- calling ----------------------------------------------------------------------------------
async def test_a_call_returns_the_result_and_posts_json_to_the_bot_path(api, fake):
    assert (await api.get_me())["username"] == "test_bot"
    assert fake.calls == [("getMe", {})]


async def test_polling_asks_for_messages_and_button_presses_only(api, fake):
    await api.get_updates(41, 50)
    [params] = fake.params("getUpdates")
    assert params == {"offset": 41, "timeout": 50, "allowed_updates": ["message", "callback_query"]}


async def test_a_trailing_slash_on_the_base_address_is_harmless(fake):
    api = TelegramApi(TOKEN, api_base="http://127.0.0.1:8081/", client=fake.client())
    # The fake asserts the path starts with /bot<token>/, so a double slash would fail here.
    await api.get_me()


async def test_messages_are_sent_without_link_previews_and_quietly_only_when_asked(api, fake):
    await api.send_message(7, "hi")
    keyboard = {"inline_keyboard": [[{"text": "Ok", "callback_data": "x"}]]}
    await api.send_message(7, "<b>x</b>", parse_mode="HTML", silent=True, reply_markup=keyboard)
    first, second = fake.sent()
    assert first == {"chat_id": 7, "text": "hi", "link_preview_options": {"is_disabled": True}}
    assert second["parse_mode"] == "HTML" and second["disable_notification"] is True
    assert second["reply_markup"] == keyboard


async def test_callback_answers_are_short_enough_for_telegram(api, fake):
    await api.answer_callback("cb1", "x" * 500)
    assert len(fake.params("answerCallbackQuery")[0]["text"]) == 190


# ---- errors ------------------------------------------------------------------------------------
async def test_telegram_errors_carry_status_description_and_wait(api, fake):
    fake.script("sendMessage", Fail(429, "Too Many Requests: retry after 7", retry_after=7))
    with pytest.raises(TelegramApiError) as caught:
        await api.send_message(7, "hi")
    error = caught.value
    assert (error.status, error.retry_after, error.method) == (429, 7.0, "sendMessage")
    assert not error.permanent


@pytest.mark.parametrize(
    ("status", "permanent"),
    [
        (400, True),
        (401, True),
        (403, True),
        (404, True),
        (409, False),
        (429, False),
        (500, False),
        (502, False),
    ],
)
async def test_which_errors_are_pointless_to_retry(api, fake, status, permanent):
    fake.script("sendMessage", Fail(status, "x"))
    with pytest.raises(TelegramApiError) as caught:
        await api.send_message(7, "hi")
    assert caught.value.permanent is permanent


async def test_markup_the_server_cannot_parse_is_recognised(api, fake):
    fake.script(
        "sendMessage", Fail(400, "Bad Request: can't parse entities: Unsupported start tag")
    )
    fake.script("sendMessage", Fail(400, "Bad Request: chat not found"))
    with pytest.raises(TelegramApiError) as bad_markup:
        await api.send_message(7, "<x>", parse_mode="HTML")
    with pytest.raises(TelegramApiError) as other:
        await api.send_message(7, "hi")
    assert bad_markup.value.unparsable and not other.value.unparsable


async def test_an_error_without_a_json_body_is_still_an_error():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text="<html>bad gateway"))
    )
    with pytest.raises(TelegramApiError) as caught:
        await TelegramApi(TOKEN, client=client).get_me()
    assert caught.value.status == 502


async def test_a_200_that_says_not_ok_is_an_error():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": False, "description": "odd"})
        )
    )
    with pytest.raises(TelegramApiError, match="odd"):
        await TelegramApi(TOKEN, client=client).get_me()


async def test_no_connection_means_nothing_was_sent_and_the_token_stays_out_of_the_message(
    api, fake
):
    fake.script(
        "sendMessage",
        httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage"),
    )
    with pytest.raises(NotSent) as caught:
        await api.send_message(7, "hi")
    assert TOKEN not in str(caught.value) and "api.telegram.org" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("t"), httpx.PoolTimeout("p")])
async def test_failing_to_get_a_connection_in_time_also_means_nothing_was_sent(api, fake, error):
    fake.script("getMe", error)
    with pytest.raises(NotSent):
        await api.get_me()


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
async def test_a_failure_after_the_request_went_out_is_not_hidden_as_not_sent(api, fake, error):
    fake.script("sendMessage", error)
    with pytest.raises(type(error)):
        await api.send_message(7, "hi")  # the outcome is unknown, and the caller must know that


def test_the_real_client_never_follows_redirects_with_the_token_in_the_url():
    assert _client(None).follow_redirects is False


# ---- downloads ---------------------------------------------------------------------------------
async def test_a_file_is_fetched_by_the_path_telegram_names(api, fake):
    fake.file("f1", b"picture bytes")
    assert await api.download("f1") == b"picture bytes"
    assert fake.count("getFile") == 1


async def test_a_file_that_is_too_large_is_refused_before_downloading(api, fake):
    fake.file("big", b"x" * 10, claimed_size=5_000)
    with pytest.raises(TelegramApiError) as caught:
        await api.download("big", max_bytes=1000)
    assert caught.value.status == 413


async def test_a_file_that_lies_about_its_size_is_cut_off_while_downloading(api, fake):
    fake.file("liar", b"x" * 5_000, claimed_size=10)
    with pytest.raises(TelegramApiError) as caught:
        await api.download("liar", max_bytes=1000)
    assert caught.value.status == 413


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "a/../b", ""])
async def test_a_file_path_that_leaves_the_file_area_is_refused(fake, path):
    fake.script("getFile", {"file_id": "x", "file_path": path})
    api = TelegramApi(TOKEN, client=fake.client())
    with pytest.raises(TelegramApiError):
        await api.download("x")


async def test_an_unknown_file_id_is_an_error(api):
    with pytest.raises(TelegramApiError):
        await api.download("nope")


async def test_a_failed_file_fetch_is_an_error(api, fake):
    fake.script("getFile", {"file_id": "gone", "file_path": "files/gone"})
    with pytest.raises(TelegramApiError) as caught:
        await api.download("gone")  # getFile worked, the file itself is a 404
    assert caught.value.status == 404
