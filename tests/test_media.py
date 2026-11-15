from __future__ import annotations

import base64
import os
import stat

import pytest

from personal_agent.core.media import (
    MISSING_PICTURE,
    OLD_PICTURE,
    SCHEME,
    MediaError,
    MediaStore,
    expand_media,
    image_part,
    sniff_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


@pytest.fixture
def store(tmp_path) -> MediaStore:
    return MediaStore(tmp_path / "media")


# ---- what counts as a picture ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "mime"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_pictures_are_recognised_by_their_bytes(data, mime):
    assert sniff_image(data) == mime


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"%PDF-1.7",
        b"MZ\x90\x00",
        b"RIFF\x00\x00\x00\x00WAVEfmt ",
    ],
)
def test_anything_else_is_refused_whatever_it_claims_to_be(data, store):
    assert sniff_image(data) is None
    with pytest.raises(MediaError):
        store.put(data)


# ---- storing ----------------------------------------------------------------------------------
def test_a_picture_is_stored_under_its_hash_and_privately(store):
    ref = store.put(PNG)
    assert ref.mime == "image/png" and ref.size == len(PNG)
    files = list(store.directory.iterdir())
    assert [f.name for f in files] == [f"{ref.id}.png"]
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert ref.to_json() == {"kind": "image", "id": ref.id, "mime": "image/png", "bytes": len(PNG)}


def test_the_same_picture_is_stored_once(store):
    assert store.put(JPEG).id == store.put(JPEG).id
    assert len(list(store.directory.iterdir())) == 1


def test_no_half_written_file_is_left_behind_if_writing_fails(store, monkeypatch):
    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.put(PNG)
    assert list(store.directory.iterdir()) == []


def test_a_stored_picture_reads_back_identically(store):
    ref = store.put(WEBP)
    assert store.read(ref.id) == (WEBP, "image/webp")
    url = store.data_url(ref.id)
    assert url == "data:image/webp;base64," + base64.b64encode(WEBP).decode()


@pytest.mark.parametrize(
    "bad",
    [
        "../secret",  # a real picture, one level above the media folder
        "../../etc/passwd",
        "a" * 63,
        "A" * 64,
        "g" * 64,
        "",
        "x/../" + "a" * 64,
        "a" * 64 + ".png",
    ],
)
def test_an_id_that_is_not_a_hash_never_reaches_the_file_system(bad, store, tmp_path):
    store.directory.mkdir(parents=True)  # ".." only resolves through a folder that exists
    (tmp_path / "secret.png").write_bytes(PNG)  # next to the media folder: reachable by "../secret"
    assert store.read(bad) is None
    assert store.data_url(bad) is None


def test_a_missing_picture_reads_as_none(store):
    assert store.read("a" * 64) is None


# ---- what the model is shown ------------------------------------------------------------------
def item(*media_ids, text="look"):
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": text}, *(image_part(i) for i in media_ids)],
    }


def fake_urls(known):
    return lambda media_id: f"data:image/png;base64,{media_id}" if media_id in known else None


def test_only_the_newest_pictures_are_sent_and_older_ones_become_a_note():
    items = [item("a"), {"role": "assistant", "content": "ok"}, item("b", "c"), item("d")]
    shown = expand_media(items, fake_urls("abcd"), keep=2)
    urls = [
        part.get("image_url")
        for it in shown
        if isinstance(it["content"], list)
        for part in it["content"]
        if part["type"] == "input_image"
    ]
    assert urls == ["data:image/png;base64,c", "data:image/png;base64,d"]
    older = [
        p["text"] for p in shown[0]["content"] + shown[2]["content"] if p["type"] == "input_text"
    ]
    assert older.count(OLD_PICTURE) == 2  # "a" and "b"


def test_keeping_none_turns_every_picture_into_a_note():
    shown = expand_media([item("a")], fake_urls("a"), keep=0)
    assert shown[0]["content"][1] == {"type": "input_text", "text": OLD_PICTURE}


def test_a_picture_that_vanished_from_disk_is_said_to_be_gone():
    shown = expand_media([item("a")], fake_urls(""), keep=2)
    assert shown[0]["content"][1] == {"type": "input_text", "text": MISSING_PICTURE}


def test_the_stored_items_are_never_changed():
    items = [item("a"), item("b")]
    snapshot = repr(items)
    expand_media(items, fake_urls("ab"), keep=1)
    assert repr(items) == snapshot
    assert items[0]["content"][1]["image_url"] == SCHEME + "a"


def test_items_without_pictures_pass_through_untouched():
    items = [
        {"role": "user", "content": "hi"},
        {"type": "function_call_output", "call_id": "1", "output": "x"},
    ]
    assert expand_media(items, fake_urls(""), keep=2) is items


def test_a_user_who_types_the_scheme_in_text_gets_no_file_read():
    # Only structured image parts are references; a typed "pa-media:..." is just text.
    items = [{"role": "user", "content": [{"type": "input_text", "text": SCHEME + "a" * 64}]}]
    assert expand_media(items, fake_urls("a" * 64), keep=2) is items
