from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import httpx
import openai
import pytest

from personal_agent.channels import transcribe as transcribe_module
from personal_agent.channels.transcribe import (
    NOMINAL_BYTES_PER_SECOND,
    OpenAITranscriber,
    TranscriptionError,
    audio_extension,
    convert_with_ffmpeg,
    estimate_seconds,
)
from personal_agent.config import MediaConfig, ModelConfig


# ---- names and lengths ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "name", "extension"),
    [
        ("audio/ogg", "", "ogg"),
        ("audio/ogg; codecs=opus", "", "ogg"),
        ("AUDIO/MPEG", "", "mp3"),
        ("audio/x-m4a", "", "m4a"),
        ("video/webm", "", "webm"),
        ("application/octet-stream", "memo.WAV", "wav"),
        ("application/octet-stream", "memo.m4a", "m4a"),
        ("application/octet-stream", "memo.not-an-extension", "ogg"),
        ("application/octet-stream", "../../etc/passwd", "ogg"),
        ("", "", "ogg"),
    ],
)
def test_the_extension_comes_from_the_type_then_the_name_then_a_safe_default(mime, name, extension):
    assert audio_extension(mime, name) == extension


def test_a_clip_with_no_length_is_estimated_from_its_size():
    assert estimate_seconds(10, 12.5) == 12.5
    assert estimate_seconds(NOMINAL_BYTES_PER_SECOND * 90, None) == 90
    assert estimate_seconds(NOMINAL_BYTES_PER_SECOND * 90, 0) == 90
    assert estimate_seconds(10, None) == 1.0  # never zero: a tiny clip still costs something


# ---- ffmpeg --------------------------------------------------------------------------------
def fake_ffmpeg(directory, body: str):
    path = directory / "ffmpeg"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return directory


WRITES_M4A = 'for last; do :; done\nprintf "CONVERTED" > "$last"\n'


async def test_audio_is_converted_with_ffmpeg_when_it_is_installed(bin_dir):
    fake_ffmpeg(bin_dir, WRITES_M4A)
    assert await convert_with_ffmpeg(b"OggS....", "ogg") == b"CONVERTED"


async def test_without_ffmpeg_there_is_no_conversion(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert await convert_with_ffmpeg(b"x", "ogg") is None


async def test_a_conversion_that_fails_is_reported_as_none(bin_dir):
    fake_ffmpeg(bin_dir, "echo 'Invalid data found' >&2\nexit 1\n")
    assert await convert_with_ffmpeg(b"junk", "ogg") is None


async def test_a_conversion_that_produces_nothing_is_reported_as_none(bin_dir):
    fake_ffmpeg(bin_dir, "exit 0\n")
    assert await convert_with_ffmpeg(b"junk", "ogg") is None


async def test_a_conversion_that_hangs_is_stopped(bin_dir, monkeypatch):
    fake_ffmpeg(bin_dir, "exec sleep 30\n")
    monkeypatch.setattr(transcribe_module, "FFMPEG_TIMEOUT_S", 0.3)
    assert await convert_with_ffmpeg(b"x", "ogg") is None


async def test_the_scratch_files_are_removed(bin_dir, tmp_path, monkeypatch):
    fake_ffmpeg(bin_dir, WRITES_M4A)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)
    await convert_with_ffmpeg(b"x", "ogg")
    assert list(scratch.iterdir()) == []


# ---- the OpenAI call -------------------------------------------------------------------------
class FakeSpeech:
    def __init__(self, text="hello world", error: Exception | None = None):
        self.calls: list[dict] = []
        self.text, self.error = text, error
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


def transcriber(client: FakeSpeech, **media) -> OpenAITranscriber:
    instance = OpenAITranscriber(ModelConfig(), MediaConfig(**media))
    instance._client = client
    return instance


def api_error(cls, status=400, message="nope"):
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    response = httpx.Response(status, request=request)
    if cls is openai.APIConnectionError:
        return cls(request=request)
    if cls is openai.APITimeoutError:
        return cls(request=request)
    return cls(message, response=response, body=None)


async def test_the_words_come_back_trimmed(bin_dir):
    client = FakeSpeech("  remind me at five \n")
    assert await transcriber(client).transcribe(b"x", mime="audio/webm") == "remind me at five"


async def test_the_configured_model_and_language_are_used():
    client = FakeSpeech()
    await transcriber(client, transcribe_model="my-model", language="zh").transcribe(
        b"x", mime="audio/mpeg"
    )
    [call] = client.calls
    assert call["model"] == "my-model" and call["language"] == "zh"
    assert call["file"] == ("voice.mp3", b"x")


async def test_no_language_is_sent_unless_one_is_configured():
    client = FakeSpeech()
    await transcriber(client).transcribe(b"x", mime="audio/mpeg")
    assert "language" not in client.calls[0]


async def test_formats_openai_accepts_are_not_converted(bin_dir):
    marker = bin_dir / "ran"
    fake_ffmpeg(bin_dir, f"touch {marker}\n")
    for mime in ("audio/webm", "audio/mpeg", "audio/mp4", "audio/wav"):
        await transcriber(FakeSpeech()).transcribe(b"x", mime=mime)
    assert not marker.exists()


async def test_an_ogg_voice_note_is_converted_first_when_ffmpeg_is_there(bin_dir):
    fake_ffmpeg(bin_dir, WRITES_M4A)
    client = FakeSpeech()
    await transcriber(client).transcribe(b"OggS", mime="audio/ogg")
    assert client.calls[0]["file"] == ("voice.m4a", b"CONVERTED")


async def test_an_ogg_voice_note_is_sent_as_it_is_when_it_cannot_be_converted(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    client = FakeSpeech()
    await transcriber(client).transcribe(b"OggS", mime="audio/ogg")
    assert client.calls[0]["file"] == ("voice.ogg", b"OggS")


@pytest.mark.parametrize(
    ("error", "expect"),
    [
        (api_error(openai.AuthenticationError, 401), "OPENAI_API_KEY"),
        (api_error(openai.RateLimitError, 429), "额度用完了"),
        (api_error(openai.InternalServerError, 500), "没连上语音服务"),
        (api_error(openai.APIConnectionError), "没连上语音服务"),
        (api_error(openai.APITimeoutError), "没连上语音服务"),
    ],
)
async def test_service_failures_become_a_sentence_for_the_owner(error, expect):
    with pytest.raises(TranscriptionError, match=expect) as caught:
        await transcriber(FakeSpeech(error=error)).transcribe(b"x", mime="audio/webm")
    assert "sk-" not in str(caught.value)


async def test_a_refused_format_suggests_ffmpeg_only_when_that_could_help(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    refused = api_error(openai.BadRequestError, 400, "Invalid file format")
    with pytest.raises(TranscriptionError) as ogg:
        await transcriber(FakeSpeech(error=refused)).transcribe(b"x", mime="audio/ogg")
    assert "ffmpeg" in str(ogg.value) and "（ogg）" in str(ogg.value)
    with pytest.raises(TranscriptionError) as mp3:
        await transcriber(FakeSpeech(error=refused)).transcribe(b"x", mime="audio/mpeg")
    assert "ffmpeg" not in str(mp3.value)


async def test_without_a_key_voice_is_refused_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY is not set"):
        await OpenAITranscriber(ModelConfig(), MediaConfig()).transcribe(b"x", mime="audio/webm")


async def test_speech_authentication_error_names_the_selected_credential():
    speech = transcriber(
        FakeSpeech(error=api_error(openai.AuthenticationError, 401)),
        transcribe_api_key_env="PA_SPEECH_AUTH",
    )
    with pytest.raises(TranscriptionError, match="PA_SPEECH_AUTH") as error:
        await speech.transcribe(b"x", mime="audio/webm")
    assert "OPENAI_API_KEY" not in str(error.value)


async def test_a_missing_text_field_is_silence_not_a_crash():
    client = FakeSpeech()
    client.text = None
    assert await transcriber(client).transcribe(b"x", mime="audio/webm") == ""
