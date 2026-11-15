"""Pictures the owner sends.

A picture is stored once, under its content hash, in a private directory. The session log refers
to it (`pa-media:<hash>`) instead of holding the bytes, so the log stays small and a copy of it
never contains a photo. Only when a request is built does a reference become an inline image, and
only for the newest few: every request re-sends what it carries, so a long conversation must not
keep paying for pictures that were only relevant a day ago.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEME = "pa-media:"
IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_ID = re.compile(r"[0-9a-f]{64}")

OLD_PICTURE = (
    "[A picture the owner sent earlier is no longer attached, to save space. "
    "Ask the owner to send it again if you need to look at it.]"
)
MISSING_PICTURE = "[A picture the owner sent earlier is no longer available.]"


class MediaError(ValueError):
    """The upload is not usable. The message is safe to show the owner."""


def sniff_image(data: bytes) -> str | None:
    """The image type by its first bytes (never by a name or a type the sender claims)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


@dataclass(frozen=True)
class MediaRef:
    id: str
    mime: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"kind": "image", "id": self.id, "mime": self.mime, "bytes": self.size}


class MediaStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def put(self, data: bytes) -> MediaRef:
        mime = sniff_image(data)
        if mime is None:
            raise MediaError("That file is not a picture I can read (JPEG, PNG, WebP or GIF).")
        digest = hashlib.sha256(data).hexdigest()
        target = self.directory / f"{digest}.{IMAGE_EXT[mime]}"
        if not target.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                self.directory.chmod(0o700)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".incoming-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        return MediaRef(digest, mime, len(data))

    def read(self, media_id: str) -> tuple[bytes, str] | None:
        """The bytes and type of a stored picture, or None if there is no such picture."""
        if not _ID.fullmatch(media_id):
            return None  # an id is a hash; anything else could be a path
        for mime, ext in IMAGE_EXT.items():
            path = self.directory / f"{media_id}.{ext}"
            try:
                return path.read_bytes(), mime
            except OSError:
                continue
        return None

    def data_url(self, media_id: str) -> str | None:
        found = self.read(media_id)
        if found is None:
            return None
        data, mime = found
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ---- what the model is shown -------------------------------------------------------------
def image_part(media_id: str) -> dict[str, str]:
    return {"type": "input_image", "image_url": SCHEME + media_id, "detail": "auto"}


def _reference(part: Any) -> str | None:
    if isinstance(part, dict) and part.get("type") == "input_image":
        url = part.get("image_url")
        if isinstance(url, str) and url.startswith(SCHEME):
            return url[len(SCHEME) :]
    return None


def expand_media(
    items: list[dict[str, Any]], data_url: Callable[[str], str | None], keep: int
) -> list[dict[str, Any]]:
    """The items as the model should see them: the newest `keep` pictures inline, older ones as
    a note. The stored items are not changed."""
    where: list[tuple[int, int, str]] = []
    for i, item in enumerate(items):
        content = item.get("content")
        if isinstance(content, list):
            for j, part in enumerate(content):
                media_id = _reference(part)
                if media_id is not None:
                    where.append((i, j, media_id))
    if not where:
        return items
    newest = {(i, j) for i, j, _ in where[len(where) - keep :]} if keep > 0 else set()
    out = list(items)
    for i, j, media_id in where:
        if out[i] is items[i]:
            out[i] = {**items[i], "content": list(items[i]["content"])}
        if (i, j) in newest:
            url = data_url(media_id)
            replacement: dict[str, Any] = (
                {**items[i]["content"][j], "image_url": url}
                if url
                else {"type": "input_text", "text": MISSING_PICTURE}
            )
        else:
            replacement = {"type": "input_text", "text": OLD_PICTURE}
        out[i]["content"][j] = replacement
    return out
