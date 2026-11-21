"""Each new space gets its own data folder and a deliberately limited configuration."""

from __future__ import annotations

import re

from personal_agent.collaboration.models import OWNER_SPACE, IdentityError
from personal_agent.config import Config
from personal_agent.paths import Paths

SPACE_ID = re.compile(r"s_[a-f0-9]{32}\Z")


def space_paths(root: Paths, space_id: str) -> Paths:
    """Generated identifiers only. Never resolve a user path into a data-directory capability."""
    if space_id == OWNER_SPACE:
        return root
    if not SPACE_ID.fullmatch(space_id):
        raise IdentityError("Invalid space identifier.")
    parent = root.home / "spaces"
    destination = parent / space_id
    for directory in (parent, destination):
        if directory.is_symlink():
            raise IdentityError("Space directories must not be symbolic links.")
        if directory.exists() and not directory.is_dir():
            raise IdentityError("Space directory is unavailable.")
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        directory.chmod(0o700)
    if (
        destination.resolve().parent != parent.resolve()
        or parent.resolve().parent != root.home.resolve()
    ):
        raise IdentityError("Space directory is outside the instance.")
    return Paths(home=destination)


def derive_space_config(root: Config) -> Config:
    """Copy operational limits/model routing, never owner capabilities or private identity.

    Each App loads preferences and skills only from its own physical root. The caller must use
    build_app(read_env=False, config=...), and disable local sandbox probing. Model inference is
    an intentionally shared service; the parent applies an aggregate instance budget.
    """
    data = root.model_dump(mode="python")
    data["agent"]["owner_name"] = None
    data["agent"]["name"] = "Assistant"
    data["tools"]["shell"] = False
    data["sandbox"]["extra_read"] = []
    data["sandbox"]["extra_write"] = []
    data["sandbox"]["backend"] = "auto"
    data["secrets"] = {}
    data["mcp"] = {"enabled": False, "servers": []}
    data["plugins"] = {"enabled": [], "settings": {}}
    data["skills"]["enabled"] = False
    data["net"]["allow_private"] = []
    data["policy"] = {
        "mode": "supervised",
        "approval_timeout_hours": root.policy.approval_timeout_hours,
    }
    data["telegram"] = {"enabled": False}
    data["dingtalk"] = {"enabled": False}
    data["webhooks"] = {"enabled": False, "routes": {}}
    data["web"]["enabled"] = False
    data["notify"]["channel"] = "web"
    data["runtime"]["concurrency"] = 1
    return Config.model_validate(data)
