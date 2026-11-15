"""Characters that hide text from a person but not from a model.

A tool description or a web page can carry instructions the owner cannot see: "tag" characters
(U+E0000 block) that render as nothing but that language models read as plain ASCII, bidirectional
overrides that make text display in a different order than it is read, and control characters
that rewrite what a terminal shows. Review is only worth something if what the owner reviews is
what the model gets, so text from outside passes through here before either of them sees it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_ALWAYS = re.compile(
    "["
    "\x00-\x08\x0b-\x1f"  # control characters (not tab and newline), including ESC and CR
    "\x7f-\x9f"  # DEL and the C1 controls (some terminals act on these)
    "\u061c\u200e\u200f"  # Arabic letter mark, left-to-right and right-to-left marks
    "\u202a-\u202e\u2066-\u2069"  # bidirectional embeddings, overrides and isolates
    "\U000e0000-\U000e007f"  # tag characters: invisible ASCII
    "\U000e0100-\U000e01ef"  # supplementary variation selectors
    "]"
)
_FORMATTING = frozenset({"Cf", "Co", "Cs", "Cn"})
"""Format (zero-width and the like), private use, surrogates, unassigned."""


def strip_invisible(text: str, *, formatting: bool = False) -> str:
    """`text` without characters that can hide or reorder it.

    Tabs and newlines stay (CRLF becomes LF; the line and paragraph separators become newlines).
    With `formatting=True` every zero-width and other format character goes as well: right for
    short descriptions that are read by the owner and the model, too blunt for prose in
    scripts that need joiners (Persian, Indic), which is why it is not the default."""
    text = text.replace("\r\n", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    text = _ALWAYS.sub("", text)
    if formatting and not text.isascii():
        text = "".join(
            ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in _FORMATTING
        )
    return text


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(text: str) -> str:
    """`text` with each lone surrogate replaced by U+FFFD (the replacement character).

    A lone surrogate is a legal Python string and legal JSON (`"\\ud83d"`: half an emoji, what a
    truncated string leaves behind: webhook payloads, web pages' JSON, tool arguments from a model),
    but it cannot be written as UTF-8. It makes saving, sending or printing the text fail, so one
    such message would otherwise be a message that can never be stored: refused again on every
    retry. This is where it stops being a problem."""
    return text if text.isascii() else _LONE_SURROGATE.sub("\ufffd", text)


def scrub_tree(value: Any) -> Any:
    """`value` (parsed JSON, or statement parameters: strings, lists, tuples, dicts, numbers) with
    `scrub_surrogates` applied to every string, keys included. Anything else comes back as it is."""
    if isinstance(value, str):
        return scrub_surrogates(value)
    if isinstance(value, list):
        return [scrub_tree(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_tree(item) for item in value)
    if isinstance(value, dict):
        return {scrub_tree(key): scrub_tree(item) for key, item in value.items()}
    return value


def hides_text(text: str) -> bool:
    """Whether `text` contains anything `strip_invisible(text, formatting=True)` would remove."""
    return strip_invisible(text, formatting=True) != text.replace("\r\n", "\n")


def fenced(tag: str, text: str) -> str:
    """`text` between <tag> lines, with nothing inside able to close the fence early.

    Used for notes the agent wrote itself (memory index, task files, a summary of its own
    conversation): they are data, and some of them were written after reading a web page, so they
    must not be able to step out of the block and speak as the prompt."""
    safe = re.sub(rf"(?i)</\s*{tag}", f"</ {tag}", text)
    return f"<{tag}>\n{safe}\n</{tag}>"
