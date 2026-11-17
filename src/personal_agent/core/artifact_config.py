"""Limits for files the agent publishes to the owner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    max_file_bytes: int = Field(10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    max_total_bytes: int = Field(200 * 1024 * 1024, ge=1)
    max_active_files: int = Field(1000, ge=1)
    retention_days: int = Field(30, ge=1, le=3650)

    @model_validator(mode="after")
    def _quota_holds_one_file(self) -> ArtifactConfig:
        if self.max_total_bytes < self.max_file_bytes:
            raise ValueError("max_total_bytes must be at least max_file_bytes")
        return self
