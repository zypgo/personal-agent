"""The terminal, made safe to show text the agent did not write.

What the terminal prints can come from outside: a message the agent was told to pass on, a note it
kept after reading a web page, a command it was asked to run, a file's name. A terminal acts on
what it is given: an escape sequence can clear the screen, move the cursor up over the line that
asked for approval and print another in its place, or change the title. Text that hides or
reorders itself (a right-to-left override) shows a command's end before its start.

So every segment of text that passes through this console has those characters removed on its way
out, after Rich has made its own styling (which it writes as escape sequences too, and which is
left alone). Nothing has to remember to clean what it prints.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Console, ConsoleOptions, RenderableType
from rich.segment import Segment

from personal_agent.security.sanitize import strip_invisible


class SafeConsole(Console):
    """A `rich` console that shows text and nothing else: whatever in it would hide, reorder or
    drive the terminal is dropped (see `strip_invisible`), in every way of printing."""

    def render(
        self, renderable: RenderableType, options: ConsoleOptions | None = None
    ) -> Iterable[Segment]:
        for segment in super().render(renderable, options):
            if segment.control or not segment.text:
                yield segment  # Rich's own cursor and style codes
                continue
            clean = strip_invisible(segment.text)
            yield segment if clean == segment.text else Segment(clean, segment.style, None)
