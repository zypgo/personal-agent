"""Turning the model's Markdown into something Telegram shows well.

Telegram takes at most 4096 characters per message and understands a small HTML subset (`<b>`,
`<i>`, `<s>`, `<code>`, `<pre>`, `<a>`, `<blockquote>`), not Markdown. Three rules, learned from how
other bots get this wrong:

1. Split the *Markdown* first, then convert each piece. Rendering expands text (`&` becomes
   `&amp;`), so cutting finished HTML risks cutting a tag in half.
2. A cut inside a code block closes the block in the first message and reopens it in the next.
3. Never trust the conversion: it produces a plain-text version of each piece as well, and the
   channel falls back to it if Telegram says it cannot parse the markup. A message that arrives
   with a few stray asterisks beats one that does not arrive.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

LIMIT = 4096
SPLIT_AT = 3500
"""Markdown budget per message: leaves room for the markup that rendering adds."""

# A fence is a few backticks (or tildes) and a short language. Anything longer is just text: a fence
# that is cut by a split is written out again at the top of the next piece, and one that could be
# thousands of characters long would make the pieces no shorter than what they came from.
_FENCE = re.compile(r"^\s*(`{3,10}|~{3,10})\s*([\w+#.-]{0,30})\s*$")
_HEADING = re.compile(r"\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_TAG = re.compile(r"</?([a-z]+)(?:\s[^>]*)?>")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
_LANGUAGE = re.compile(r"[A-Za-z0-9_+#.-]{1,30}")
_ALLOWED_TAGS = frozenset({"b", "i", "s", "code", "pre", "a", "blockquote"})


def _heading(line: str) -> str | None:
    """The text of a `# heading` line, without the closing `##` and the spaces around it, or None.
    Done by hand: one pattern for the closing marks and the spaces is cubic in the length of a
    line with a long run of spaces in it (a line of 3,000 characters took half a minute)."""
    opened = _HEADING.match(line)
    if opened is None:
        return None
    text = line[opened.end() :].rstrip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1] in " \t"):  # `C#` keeps its #; `## a ##` loses them
        text = bare.rstrip()
    return text


@dataclass(frozen=True)
class Piece:
    html: str
    """Ready for `parse_mode=HTML`."""
    plain: str
    """The Markdown source of the piece, for sending as plain text if the HTML is refused."""


# ---- splitting ---------------------------------------------------------------------------
def _hard_wrap(line: str, limit: int) -> list[str]:
    """Cut one over-long line at spaces where possible."""
    pieces: list[str] = []
    while len(line) > limit:
        cut = line.rfind(" ", limit // 2, limit)
        cut = cut + 1 if cut > 0 else limit
        pieces.append(line[:cut])
        line = line[cut:]
    pieces.append(line)
    return pieces


def split_markdown(text: str, limit: int = SPLIT_AT) -> list[str]:
    """`text` in pieces of at most about `limit` characters, cut at a blank line when there is
    one in the second part of a piece, otherwise between lines, otherwise inside a long line.
    A code block that is cut is closed in the first piece and reopened in the next, so every
    piece stands on its own."""
    if len(text) <= limit:
        return [text]
    # Every line, with the code fence that is open once the line has been read.
    items: list[tuple[str, tuple[str, str] | None]] = []
    fence: tuple[str, str] | None = None
    for original in text.split("\n"):
        for line in _hard_wrap(original, limit):
            match = _FENCE.match(line) if line is original else None
            if match is not None:
                if fence is None:
                    fence = (match.group(1), match.group(2))
                elif match.group(1)[0] == fence[0][0] and not match.group(2):
                    fence = None
            items.append((line, fence))

    pieces: list[str] = []
    start = 0
    reopen: tuple[str, str] | None = None
    while start < len(items):
        lines = [f"{reopen[0]}{reopen[1]}"] if reopen else []
        size = sum(len(x) + 1 for x in lines)
        end = start
        while end < len(items):
            line, state = items[end]
            closing = len(state[0]) + 1 if state else 0  # needed if the piece stops after this line
            if end > start and size + len(line) + 1 + closing > limit:
                break
            size += len(line) + 1
            end += 1
        next_start = end
        if end < len(items):
            floor = start + (end - start) // 3
            for k in range(end - 1, floor, -1):
                if not items[k][0].strip() and items[k][1] is None:
                    end, next_start = k, k + 1  # cut at the blank line and drop it
                    break
        lines += [line for line, _ in items[start:end]]
        state = items[end - 1][1] if end > start else None
        if state is not None:
            lines.append(state[0])  # close the block we are cutting through
        pieces.append("\n".join(lines))
        reopen = state
        start = next_start
    return [piece for piece in pieces if piece.strip()] or [""]


# ---- converting --------------------------------------------------------------------------
def _inline(text: str) -> str:
    """One line of Markdown as Telegram HTML."""
    out: list[str] = []
    last = 0
    for match in _CODE_SPAN.finditer(text):
        out.append(_emphasis(text[last : match.start()]))
        out.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        last = match.end()
    out.append(_emphasis(text[last:]))
    return "".join(out)


def _emphasis(segment: str) -> str:
    if not segment:
        return ""
    text = html.escape(segment, quote=False)
    saved: list[str] = []

    def keep(markup: str) -> str:
        saved.append(markup)
        return f"\x00{len(saved) - 1}\x00"

    def link(match: re.Match[str]) -> str:
        url = match.group(2).replace('"', "%22")
        return keep(f'<a href="{url}">') + match.group(1) + keep("</a>")

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDERSCORE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text)


def _code_block(lines: list[str], language: str) -> str:
    body = html.escape("\n".join(lines), quote=False)
    if language and _LANGUAGE.fullmatch(language):
        return f'<pre><code class="language-{language}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def balanced(markup: str) -> bool:
    """Whether every tag in `markup` is closed in order and only allowed tags are used."""
    stack: list[str] = []
    for match in _TAG.finditer(markup):
        name = match.group(1)
        if name not in _ALLOWED_TAGS:
            return False
        if match.group(0).startswith("</"):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def to_html(markdown: str) -> str:
    """Markdown (the subset models actually write) as Telegram HTML. May return markup that is not
    well formed for unusual input; `balanced()` tells, and the caller falls back to plain text."""
    lines = markdown.replace("\x00", "").replace("\r\n", "\n").split("\n")  # NUL: see `_emphasis`
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence is not None:
            marker, language = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines):
                closing = _FENCE.match(lines[i])
                if closing and closing.group(1)[0] == marker[0] and not closing.group(2):
                    break
                body.append(lines[i])
                i += 1
            out.append(_code_block(body, language))
            i += 1
            continue
        if _TABLE_ROW.match(line):
            rows: list[str] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            out.append(_code_block(rows, ""))
            continue
        if _QUOTE.match(line):
            quoted: list[str] = []
            while i < len(lines) and (m := _QUOTE.match(lines[i])):
                quoted.append(_inline(m.group(1)))
                i += 1
            out.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            continue
        if _RULE.match(line):
            out.append("──────────")
        elif (heading := _heading(line)) is not None:
            out.append(f"<b>{_inline(heading)}</b>")
        elif bullet := _BULLET.match(line):
            out.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)


def _plain_pieces(text: str) -> list[Piece]:
    """`text` as it is, in pieces that stay under `LIMIT` however much escaping makes of them (a
    character grows to at most five: `&` becomes `&amp;`)."""
    return [Piece(html.escape(part, quote=False), part) for part in _hard_wrap(text, LIMIT // 5)]


def render(text: str) -> list[Piece]:
    """The message as pieces ready to send, each short enough for Telegram."""
    pieces: list[Piece] = []
    # NUL stands in for a link's markup while a line is converted; a NUL in the text would be taken
    # for one of those stand-ins. Telegram cannot show it anyway.
    pending = split_markdown(text.replace("\x00", "").strip() or "(empty message)")
    while pending:
        chunk = pending.pop(0)
        markup = to_html(chunk)
        if len(markup) > LIMIT or len(chunk) > LIMIT:
            # Too long once rendered: cut the Markdown smaller and retry, unless that makes no
            # headway (then nothing short of plain text will do).
            smaller = split_markdown(chunk, max(100, len(chunk) // 2)) if len(chunk) > 200 else []
            if smaller and max(len(piece) for piece in smaller) < len(chunk):
                pending = [*smaller, *pending]
            else:
                pieces.extend(_plain_pieces(chunk))
            continue
        if balanced(markup):
            pieces.append(Piece(markup, chunk))
        else:
            pieces.extend(_plain_pieces(chunk))
    return pieces
