"""DingTalk's own configuration; independent of the application config module."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator


class DingTalkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    client_id_env: str = "DINGTALK_CLIENT_ID"
    client_secret_env: str = "DINGTALK_CLIENT_SECRET"  # noqa: S105 - an environment variable name
    robot_code: str | None = None
    """The application's RobotCode. None uses its ClientID (AppKey)."""
    corp_id: str | None = None
    """Optionally restrict the robot to this organization. Cross-organization chats are refused."""

    @field_validator("client_id_env", "client_secret_env")
    @classmethod
    def _environment_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", value):
            raise ValueError("use an environment variable name, not the credential itself")
        return value

    @field_validator("robot_code", "corp_id")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if (
                not value
                or len(value) > 256
                or any(c.isspace() or not c.isprintable() for c in value)
            ):
                raise ValueError("use a nonempty DingTalk identifier without whitespace")
        return value
