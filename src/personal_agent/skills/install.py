"""Putting skills into the skills directory, and taking them out.

Installing is the owner's decision, never the model's: a skill is instructions (and sometimes
scripts) that every later run will trust. So installation is a copy of a *reviewed* folder:

- the folder is inspected first and refused if it is not a valid skill, contains symbolic links,
  special files, or is unreasonably large;
- the owner sees what is in it (and which files are scripts) before it is copied;
- the copy replaces an existing skill only when asked to, and is swapped in atomically.

A repository can be installed from an https address; it is cloned shallowly into a temporary folder
with every other git transport disabled, and the folder inside it is then treated like any other.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.config import SkillsConfig
from personal_agent.parsing import loads_json
from personal_agent.paths import Paths
from personal_agent.skills.parse import SkillFormatError, parse_skill
from personal_agent.skills.store import SkillStore, read_skill_file

MAX_FILES = 500
MAX_BYTES = 20_000_000
GIT_TIMEOUT_S = 120
SCRIPT_SUFFIXES = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".ts",
        ".rb",
        ".pl",
        ".php",
        ".ps1",
        ".bat",
        ".command",
    }
)
IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "node_modules")
MARKER = ".bundled.json"


class InstallError(Exception):
    """The skill was not installed; the message says why."""


@dataclass
class Review:
    """What the owner is shown before a skill is copied in."""

    folder: Path
    name: str = ""
    description: str = ""
    files: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    total_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_script(path: Path, mode: int) -> bool:
    return (
        path.suffix.lower() in SCRIPT_SUFFIXES
        or bool(mode & stat.S_IXUSR)
        or "scripts" in path.parts
    )


def inspect_folder(folder: Path) -> Review:
    """Check a skill folder without changing anything."""
    review = Review(folder=folder)
    if not folder.is_dir() or folder.is_symlink():
        review.errors.append(f"{folder} is not a folder")
        return review
    try:
        parsed = parse_skill(read_skill_file(folder / "SKILL.md"), folder_name=folder.name)
    except SkillFormatError as exc:
        review.errors.append(str(exc))
    else:
        review.name, review.description = parsed.name, parsed.description
        review.warnings.extend(parsed.warnings)

    links: list[str] = []
    for current, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__"}]
        base = Path(current)
        for entry in [*dirs, *names]:
            path = base / entry
            relative = path.relative_to(folder).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                links.append(relative)
            elif stat.S_ISREG(mode):
                review.files.append(relative)
                review.total_bytes += path.lstat().st_size
                if _is_script(Path(relative), mode):
                    review.scripts.append(relative)
            elif not stat.S_ISDIR(mode):
                review.errors.append(f"{relative} is a special file (socket, device or pipe)")
    if links:
        shown = ", ".join(links[:5]) + (" ..." if len(links) > 5 else "")
        review.errors.append(f"contains symbolic links, which are not installed: {shown}")
    if len(review.files) > MAX_FILES:
        review.errors.append(f"{len(review.files)} files; the limit is {MAX_FILES}")
    if review.total_bytes > MAX_BYTES:
        review.errors.append(f"{review.total_bytes:,} bytes; the limit is {MAX_BYTES:,}")
    review.files.sort()
    review.scripts.sort()
    return review


def install_folder(paths: Paths, folder: Path, *, force: bool = False) -> Path:
    """Copy a reviewed skill folder into the skills directory; returns where it went."""
    review = inspect_folder(folder)
    if not review.ok:
        raise InstallError("; ".join(review.errors))
    root = paths.skills_dir
    root.mkdir(parents=True, exist_ok=True)
    target = root / review.name
    if target.exists() and not force:
        raise InstallError(
            f"a skill named {review.name!r} is already installed (use --force to replace it)"
        )

    staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    old: Path | None = None
    try:
        copy = staging / review.name
        # Links are copied as links, never followed; the copy is then inspected again, so a link
        # that appeared after the review is caught instead of installed.
        shutil.copytree(folder, copy, ignore=IGNORED, symlinks=True)
        again = inspect_folder(copy)
        if not again.ok:
            raise InstallError("; ".join(again.errors))
        _tighten(copy)
        if target.exists():
            old = staging / "previous"
            target.rename(old)
        copy.rename(target)
    except OSError as exc:
        if old is not None and not target.exists():
            old.rename(target)  # put the previous version back
        raise InstallError(f"could not copy the skill: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def _tighten(folder: Path) -> None:
    """Owner-only permissions; execute bits are kept where they were set."""
    for current, _dirs, names in os.walk(folder):
        os.chmod(current, 0o700)
        for name in names:
            path = Path(current) / name
            mode = path.stat().st_mode
            os.chmod(path, 0o700 if mode & stat.S_IXUSR else 0o600)


def remove_skill(paths: Paths, name: str) -> Path:
    """Delete an installed skill, found by its name (or its folder name)."""
    scan = SkillStore(paths, SkillsConfig()).scan()
    skill = scan.by_name().get(name)
    folder = skill.directory if skill is not None else paths.skills_dir / name
    root = paths.skills_dir.resolve()
    real = folder.resolve() if folder.exists() else folder
    if not folder.exists() or folder.is_symlink() or real.parent != root:
        raise InstallError(f"no installed skill named {name!r}")
    shutil.rmtree(folder)
    return folder


# ---- from a repository ------------------------------------------------------------------------
def check_git_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise InstallError(
            "only plain https:// repository addresses (without credentials) are accepted"
        )
    if any(c.isspace() or ord(c) < 32 for c in url) or url.startswith("-"):
        raise InstallError("that does not look like a repository address")
    return url


@contextlib.contextmanager
def fetched(url: str, subpath: str | None = None) -> Iterator[Path]:
    """A shallow clone of an https repository; yields the skill folder inside it."""
    check_git_url(url)
    git = shutil.which("git")
    if git is None:
        raise InstallError("git is not installed; download the folder and install it from disk")
    with tempfile.TemporaryDirectory(prefix="pa-skill-") as tmp:
        clone = Path(tmp) / "repo"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        command = [
            git,
            "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", "core.symlinks=false",
            "clone", "--depth", "1", "--quiet", "--", url, str(clone),
        ]  # fmt: skip
        try:
            done = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise InstallError(f"cloning took longer than {GIT_TIMEOUT_S} seconds") from exc
        if done.returncode != 0:
            detail = " ".join(done.stderr.split())[:200]
            raise InstallError(f"git could not fetch {url}: {detail}")
        yield _inside(clone, subpath)


def _inside(clone: Path, subpath: str | None) -> Path:
    if subpath:
        target = (clone / subpath).resolve()
        if not target.is_relative_to(clone.resolve()):
            raise InstallError("--path must point inside the repository")
        if not (target / "SKILL.md").is_file():
            raise InstallError(f"{subpath} has no SKILL.md")
        return target
    if (clone / "SKILL.md").is_file():
        return clone
    candidates = sorted(
        p.parent.relative_to(clone).as_posix() for p in clone.glob("*/SKILL.md")
    ) or sorted(p.parent.relative_to(clone).as_posix() for p in clone.glob("skills/*/SKILL.md"))
    if candidates:
        shown = ", ".join(candidates[:12]) + (" ..." if len(candidates) > 12 else "")
        raise InstallError(
            f"this repository holds several skills ({shown}); choose one with --path"
        )
    raise InstallError("no SKILL.md found in the repository")


# ---- starter skills ---------------------------------------------------------------------------
def _bundled_root() -> Traversable:
    return resources.files("personal_agent.skills") / "bundled"


def bundled_names() -> list[str]:
    root = _bundled_root()
    return sorted(e.name for e in root.iterdir() if e.is_dir() and (e / "SKILL.md").is_file())


def _copy_traversable(source: Traversable, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in {"__pycache__", ".DS_Store"}:
            continue
        if entry.is_dir():
            _copy_traversable(entry, target / entry.name)
        else:
            (target / entry.name).write_bytes(entry.read_bytes())


def install_bundled(paths: Paths) -> list[str]:
    """Copy the starter skills that came with the program, once. A skill the owner deleted is
    remembered and not brought back; one that exists is never overwritten."""
    root = paths.skills_dir
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    try:
        done = set(loads_json(marker.read_text(encoding="utf-8")).get("installed", []))
    except (OSError, ValueError, AttributeError, TypeError):
        done = set()
    added: list[str] = []
    for name in bundled_names():
        if name in done:
            continue
        target = root / name
        if not target.exists():
            _copy_traversable(_bundled_root() / name, target)
            _tighten(target)
            added.append(name)
        done.add(name)
    marker.write_text(json.dumps({"installed": sorted(done)}, indent=2) + "\n", encoding="utf-8")
    return added
