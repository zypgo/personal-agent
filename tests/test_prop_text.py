"""Text that cannot be written as UTF-8 must not be able to stop the agent.

A lone surrogate is half an emoji. It is a legal Python string and legal JSON (`"\\ud83d"`), and it
is what a truncated string leaves behind: a webhook payload whose commit title was cut by a byte
limit, a web page's JSON, tool arguments from a model. SQLite and the file system refuse to store
it, so before this was handled one such message could never be saved, and the same event failed
again on every retry.
"""

from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from hypothesis import given
from hypothesis import strategies as st

from helpers import owner_message, usage, webhook
from personal_agent.core.worker import Worker
from personal_agent.db import dumps, loads, open_database
from personal_agent.harness.runctx import RunContext
from personal_agent.memory.store import Actor
from personal_agent.security.sanitize import scrub_surrogates, scrub_tree
from personal_agent.tools.base import Category, ToolDef
from tooling import drain, outputs

HALF = "\ud83d"  # the first half of a surrogate pair, on its own
BAD = f"before {HALF} after"
CLEAN = "before \ufffd after"

SURROGATES = st.sampled_from(["\ud800", "\ud83d", "\ude00", "\udc00", "\udfff"])
TEXT = st.lists(
    st.one_of(st.text(max_size=8), SURROGATES, st.sampled_from(["\U0001f600", "\u4f60\u597d"])),
    max_size=12,
).map("".join)


def encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ---- the scrubber ---------------------------------------------------------------------------
@given(TEXT)
def test_scrubbed_text_can_always_be_written_and_scrubbing_twice_changes_nothing(text):
    once = scrub_surrogates(text)
    assert encodable(once)
    assert scrub_surrogates(once) == once
    assert len(once) == len(text)  # one replacement character per lone surrogate


@given(st.text(max_size=60))
def test_text_that_was_fine_is_left_alone(text):
    assert scrub_surrogates(text) == text


def test_a_character_outside_the_basic_plane_is_not_a_lone_surrogate():
    assert scrub_surrogates("\U0001f600") == "\U0001f600"


@given(
    st.recursive(
        st.one_of(TEXT, st.integers(), st.none(), st.booleans()),
        lambda inner: st.one_of(
            st.lists(inner, max_size=4),
            st.dictionaries(TEXT, inner, max_size=4),
            st.lists(inner, max_size=4).map(tuple),
        ),
        max_leaves=12,
    )
)
def test_a_whole_structure_comes_out_writable(value):
    def check(item):
        if isinstance(item, str):
            assert encodable(item)
        elif isinstance(item, dict):
            for key, inner in item.items():
                check(key)
                check(inner)
        elif isinstance(item, list | tuple):
            for inner in item:
                check(inner)

    check(scrub_tree(value))


# ---- the database --------------------------------------------------------------------------
@given(TEXT, TEXT)
def test_the_database_keeps_any_text_with_the_broken_halves_replaced(key, value):
    db = open_database(":memory:")
    try:
        db.kv_set(key, value)
        assert db.kv_get(scrub_surrogates(key)) == scrub_surrogates(value)
        db.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (:k, :v, 0)",
            {"k": f"{key}!", "v": value},
        )
        assert db.scalar("SELECT value FROM kv WHERE key = ?", (f"{key}!",)) == scrub_surrogates(
            value
        )
        assert db.query(
            "SELECT key FROM kv WHERE key = ?", (key,)
        )  # a lookup by broken text works too
    finally:
        db.close()


@given(
    st.recursive(
        TEXT,
        lambda inner: st.lists(inner, max_size=3) | st.dictionaries(TEXT, inner, max_size=3),
        max_leaves=8,
    )
)
def test_json_with_a_broken_half_is_saved_and_read_back(value):
    db = open_database(":memory:")
    try:
        db.kv_set("blob", dumps(value))
        assert loads(db.kv_get("blob")) == scrub_tree(json.loads(dumps(value)))
    finally:
        db.close()


def test_a_batch_of_rows_is_saved_whole_or_not_at_all():
    db = open_database(":memory:")
    try:
        db.executemany(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, 0)",
            [("a", "1"), ("b", BAD), ("c", "3")],
        )
        assert {row["key"]: row["value"] for row in db.query("SELECT key, value FROM kv")} == {
            "a": "1",
            "b": CLEAN,
            "c": "3",
        }
    finally:
        db.close()


# ---- through the whole agent --------------------------------------------------------------------
async def test_a_message_with_half_an_emoji_is_saved_answered_and_not_retried_forever(make_app):
    model = ScriptedModel([[assistant_message("fine")]])
    app = make_app(model)
    app.inbox.add(owner_message(BAD))

    [result] = await drain(app)

    assert result.outcome.status == "completed"
    assert app.inbox.counts() == {"pending": 0, "leased": 0, "done": 1, "dead": 0}
    assert CLEAN in json.dumps(model.calls[0].input, ensure_ascii=False)


async def test_a_reply_with_half_an_emoji_reaches_the_owner(make_app):
    app = make_app(ScriptedModel([[assistant_message(BAD)]]))
    app.inbox.add(owner_message("hello"))

    [result] = await drain(app)

    assert result.outcome.status == "completed"
    [message] = app.outbox.list()
    assert message.text == CLEAN
    assert encodable(message.text)


async def test_a_webhook_with_half_an_emoji_in_its_payload_becomes_an_event(make_app):
    app = make_app(ScriptedModel([[assistant_message("noted")]]))
    app.inbox.add(webhook("github", {"commit": {"title": BAD}}))

    [result] = await drain(app, session_key="webhook:github")

    assert result.outcome.status in ("completed", "silent")
    assert app.inbox.counts() == {"pending": 0, "leased": 0, "done": 1, "dead": 0}
    assert CLEAN in json.dumps(app.sessions.items("webhook:github"), ensure_ascii=False)


async def test_half_an_emoji_the_model_writes_into_a_tool_call_fails_the_attempt_not_the_event(
    make_app,
):
    # The Agents SDK itself cannot read such arguments (it fails while fingerprinting the call,
    # before any code of ours runs), so the attempt fails; the event stays to be retried, and the
    # model will not write the same thing twice.
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "memory",
                        {"command": "create", "path": "/memories/state/note.md", "file_text": BAD},
                        call_id="c1",
                    )
                ],
                usage=usage(),
            ),
            [assistant_message("saved")],
            [assistant_message("saved, differently")],
        ]
    )
    app = make_app(model)
    app.inbox.add(owner_message("write it down"))

    first = await Worker(app).run_once()
    assert first is not None and first.outcome is None
    counts = app.inbox.counts()
    assert counts["dead"] == 0 and counts["pending"] == 1


async def test_a_tool_result_with_half_an_emoji_reaches_the_model_repaired(make_app):
    @function_tool(name_override="broken", description_override="Returns half an emoji.")
    async def broken(ctx: RunContextWrapper[RunContext]) -> str:
        return BAD

    app = make_app(
        ScriptedModel([[function_call("broken", {}, call_id="c1")], [assistant_message("ok")]])
    )
    app.tools.register(ToolDef(name="broken", category=Category.READ, tool=broken))
    app.inbox.add(owner_message("go"))

    [result] = await drain(app)

    assert result.outcome.status == "completed"
    assert outputs(app)["c1"] == CLEAN


# ---- the memory store, used directly ------------------------------------------------------------
def test_the_memory_store_writes_text_with_half_an_emoji(make_app):
    app = make_app()
    memory = app.memory
    assert memory.create("/memories/state/a.md", BAD).startswith("File created")
    assert memory.read_text("/memories/state/a.md") == CLEAN
    assert memory.insert("/memories/state/a.md", 0, f"{HALF} first").startswith("The file")
    assert memory.read_text("/memories/state/a.md") == f"\ufffd first\n{CLEAN}"
    assert memory.str_replace("/memories/state/a.md", "first", f"x{HALF}y").startswith(
        "The memory file"
    )
    memory.write_text("/memories/state/b.md", BAD, actor=Actor.SYSTEM)
    assert memory.read_text("/memories/state/b.md") == CLEAN
