"""Voice messages become text before the agent sees them.

The speech model is a *port* (`Transcriber`): the default talks to OpenAI's transcription API, and
tests or other providers plug in anything with the same method. Audio is never kept: only the
words are, so a voice note leaves no recording on this machine.

OpenAI documents `mp3 mp4 mpeg mpga m4a wav webm` as accepted input. Telegram voice notes are
Ogg/Opus, which is not on that list, so when `ffmpeg` is installed those are converted first; when
it is not, the file is sent as it is and a refusal comes back with a hint instead of a guess.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol

import openai

from personal_agent.config import MediaConfig, ModelConfig
from personal_agent.harness.endpoints import transcription_endpoint
from personal_agent.harness.errors import ModelUnavailable
from personal_agent.harness.models import make_client

log = logging.getLogger(__name__)

ACCEPTED = frozenset({"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm"})
_EXT_BY_MIME = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/flac": "flac",
    "audio/amr": "amr",
}
FFMPEG_TIMEOUT_S = 60.0
NOMINAL_BYTES_PER_SECOND = 8_000
"""Used to guess the length of a clip whose sender did not say (64 kbit/s)."""


class TranscriptionError(Exception):
    """Speech-to-text did not work. The message is safe to show the owner."""


class Transcriber(Protocol):
    async def transcribe(self, data: bytes, *, mime: str, name: str = "") -> str:
        """The words in `data`. Raises `TranscriptionError` with a message for the owner."""
        ...


def audio_extension(mime: str, name: str = "") -> str:
    """The file extension for an audio upload: from its type, else from its name."""
    base = mime.split(";", 1)[0].strip().lower()
    if base in _EXT_BY_MIME:
        return _EXT_BY_MIME[base]
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if suffix.isalnum() and 1 <= len(suffix) <= 5 else "ogg"


def estimate_seconds(size: int, reported: float | None) -> float:
    if reported is not None and reported > 0:
        return float(reported)
    return max(1.0, size / NOMINAL_BYTES_PER_SECOND)


async def convert_with_ffmpeg(data: bytes, extension: str) -> bytes | None:
    """The audio as AAC in an m4a file, or None if ffmpeg is missing or could not do it."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        return None
    with tempfile.TemporaryDirectory(prefix="pa-audio-") as scratch:
        source, target = Path(scratch) / f"in.{extension}", Path(scratch) / "out.m4a"
        source.write_bytes(data)
        process = await asyncio.create_subprocess_exec(
            binary,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
            str(target),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), FFMPEG_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            log.warning("ffmpeg took longer than %.0f seconds", FFMPEG_TIMEOUT_S)
            return None
        if process.returncode != 0 or not target.exists():
            log.warning(
                "ffmpeg could not convert the audio: %s", stderr.decode(errors="replace")[:300]
            )
            return None
        return target.read_bytes()


class OpenAITranscriber:
    """OpenAI's `/audio/transcriptions`. The client is created on first use."""

    def __init__(self, model_cfg: ModelConfig, media_cfg: MediaConfig) -> None:
        self.model_cfg = model_cfg
        self.media_cfg = media_cfg
        self._client: openai.AsyncOpenAI | None = None

    def _client_or_raise(self) -> openai.AsyncOpenAI:
        if self._client is None:
            try:
                self._client = make_client(
                    self.model_cfg,
                    endpoint=transcription_endpoint(self.model_cfg, self.media_cfg),
                    timeout=60.0,
                    max_retries=2,
                )
            except ModelUnavailable as exc:
                raise TranscriptionError(f"没法转写语音：{exc}") from exc
        return self._client

    async def aclose(self) -> None:
        """Close the client's connections (see `OpenAIModels.aclose`)."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def transcribe(self, data: bytes, *, mime: str, name: str = "") -> str:
        extension = audio_extension(mime, name)
        if extension not in ACCEPTED:
            converted = await convert_with_ffmpeg(data, extension)
            if converted is not None:
                data, extension = converted, "m4a"
        client = self._client_or_raise()
        options: dict[str, Any] = {}
        if self.media_cfg.language:
            options["language"] = self.media_cfg.language
        try:
            result = await client.audio.transcriptions.create(
                model=self.media_cfg.transcribe_model,
                file=(f"voice.{extension}", data),
                **options,
            )
        except openai.AuthenticationError as exc:
            key_env = transcription_endpoint(self.model_cfg, self.media_cfg).api_key_env
            raise TranscriptionError(f"语音服务拒绝了这个 API 密钥，请检查 {key_env}。") from exc
        except openai.BadRequestError as exc:
            hint = (
                "安装 ffmpeg（例如 `brew install ffmpeg`），语音就能先转换格式。"
                if extension not in ACCEPTED
                else ""
            )
            log.warning("speech-to-text rejected the audio: %s", exc)
            raise TranscriptionError(f"语音服务不接受这段音频（{extension}）。{hint}") from exc
        except openai.RateLimitError as exc:
            raise TranscriptionError("语音服务正忙，或者额度用完了。过一分钟再试。") from exc
        except (openai.APIConnectionError, openai.APITimeoutError, openai.APIStatusError) as exc:
            log.warning("speech-to-text failed: %s", exc)
            raise TranscriptionError("没连上语音服务。可以再发一次，或者直接打字。") from exc
        return (getattr(result, "text", "") or "").strip()
