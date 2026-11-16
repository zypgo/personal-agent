"""Where the agent keeps its state on disk.

Everything personal lives under one directory (`PA_HOME`, default `~/.personal-agent`),
never inside the source repository.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "~/.personal-agent"


@dataclass(frozen=True)
class Paths:
    home: Path

    # ---- files -----------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def db_file(self) -> Path:
        return self.home / "state.db"

    # ---- directories ------------------------------------------------------
    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def preferences_dir(self) -> Path:
        """Owned by the user. The agent can read but never write here."""
        return self.memory_dir / "preferences"

    @property
    def state_dir(self) -> Path:
        """Owned by the agent: notes, bookmarks, ledgers, proposals."""
        return self.memory_dir / "state"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def workspace_dir(self) -> Path:
        """The only place shell/file tools may write."""
        return self.home / "workspace"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def media_dir(self) -> Path:
        """Pictures the owner sent, stored by content hash."""
        return self.home / "media"

    @property
    def backups_dir(self) -> Path:
        """Copies of the database that `pa db backup` makes. Created when the first one is."""
        return self.home / "backups"

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> Paths:
        raw = home or os.environ.get("PA_HOME") or DEFAULT_HOME
        return cls(home=Path(raw).expanduser().resolve())

    def ensure(self) -> None:
        """Create the directory layout with owner-only permissions."""
        for directory in (
            self.home,
            self.memory_dir,
            self.preferences_dir,
            self.state_dir,
            self.skills_dir,
            self.workspace_dir,
            self.logs_dir,
            self.run_dir,
            self.media_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    @property
    def private_files(self) -> list[Path]:
        """What holds the owner's secrets and conversations: the key file, and the database with
        the write-ahead log SQLite keeps beside it."""
        beside = [self.db_file.with_name(self.db_file.name + suffix) for suffix in ("-wal", "-shm")]
        return [self.env_file, self.db_file, *beside]

    def exposed_files(self) -> list[Path]:
        """The private files that other users can read."""
        found = []
        for path in self.private_files:
            try:
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    found.append(path)
            except OSError:
                continue  # not there (yet)
        return found

    def tighten(self) -> list[Path]:
        """Make the private files readable by their owner only. Returns the ones that were not."""
        changed = []
        for path in self.exposed_files():
            try:
                path.chmod(0o600)
            except OSError:
                continue
            changed.append(path)
        return changed

    def env_file_warnings(self) -> list[str]:
        """Human-readable problems with the `.env` file permissions (empty if fine)."""
        if not self.env_file.exists():
            return []
        mode = stat.S_IMODE(self.env_file.stat().st_mode)
        if mode & 0o077:
            return [
                f"{self.env_file} is readable by other users (mode {mode:04o}); run: chmod 600 {self.env_file}"
            ]
        return []
