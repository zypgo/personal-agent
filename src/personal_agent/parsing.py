"""Readers for JSON, YAML and TOML that report a document nested too deeply the way they report any
other bad document.

All three readers recurse, and Python answers a nesting of a few thousand levels with
`RecursionError`, which is not a `ValueError` and so sails past every `except ValueError` that was
written for "the text is not valid". A web server, a webhook sender or a downloaded skill can send
such a document at no cost; a model can write one as tool arguments. Each reader here raises the
error type its library already raises, so existing handlers keep working.
"""

from __future__ import annotations

import functools
import json
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import yaml

TOO_DEEP = "nested too deeply"


def loads_json(text: str | bytes) -> Any:
    """`json.loads`, raising `ValueError` (as `json.JSONDecodeError` does) for too deep a nesting."""
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError(TOO_DEEP) from exc


@functools.cache
def _flat_loader() -> type[yaml.SafeLoader]:
    import yaml  # not at the top: most commands never read YAML, and the import is not free

    class FlatLoader(yaml.SafeLoader):
        """`SafeLoader` that refuses aliases (`*name`).

        A document of a few hundred bytes can define each list as nine references to the one
        before ("billion laughs"); the loader shares them, so loading is instant, but anything
        that then walks, prints or validates the result takes longer than the machine has. Nothing
        this program reads (skill front matter, scenarios) has a use for an alias."""

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                mark = self.peek_event().start_mark  # type: ignore[no-untyped-call]  # stub gap
                raise yaml.constructor.ConstructorError(
                    None, None, "aliases (*name) are not allowed", mark
                )
            return super().compose_node(parent, index)

    return FlatLoader


def load_yaml(text: str) -> Any:
    """`yaml.safe_load` without aliases, raising `yaml.YAMLError` for them and for too deep a
    nesting."""
    import yaml

    try:
        return yaml.load(text, Loader=_flat_loader())  # noqa: S506 (a safe loader, see above)
    except RecursionError as exc:
        raise yaml.YAMLError(TOO_DEEP) from exc


def loads_toml(text: str) -> dict[str, Any]:
    """`tomllib.loads`, raising `tomllib.TOMLDecodeError` for too deep a nesting."""
    try:
        return tomllib.loads(text)
    except RecursionError as exc:
        raise tomllib.TOMLDecodeError(TOO_DEEP) from exc
