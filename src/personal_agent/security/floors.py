"""Shell commands that never run, whatever the mode, the rules or the owner's approval.

Only the ones that wreck a machine: formatting or repartitioning a disk, `rm -rf` on the root, a
home directory or a system directory, a fork bomb, and writing straight to a disk device. Anything
else goes on to the policy, which asks the owner, and to the sandbox, which limits what a command
can reach.

It reads the command text, so it catches commands that say what they do. Quotes, backslashes and
`${IFS}` are stripped before matching, so `r"m" -rf /` and `rm${IFS}-rf${IFS}/` are seen for what
they are, and a harmless command that merely *mentions* a dangerous one is refused too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DENIED_SUFFIX = "; this is blocked and cannot be approved"

# Programs that are refused whatever their arguments (matched on the program's base name).
FORBIDDEN_PROGRAMS = {
    "fdisk": "formatting or repartitioning a disk",
    "gdisk": "formatting or repartitioning a disk",
    "parted": "formatting or repartitioning a disk",
    "wipefs": "formatting or repartitioning a disk",
    "mke2fs": "formatting a disk",
}
FORBIDDEN_PREFIXES = {
    "mkfs": "formatting a disk",  # mkfs, mkfs.ext4, ...
    "newfs": "formatting a disk",  # newfs, newfs_apfs, ...
}
DISKUTIL_VERBS = (
    "erase",
    "partition",
    "repartition",
    "reformat",
    "zero",
    "random",
    "secure",
    "apfs",
)

# Directories whose recursive deletion is never acceptable.
TOP_LEVEL_DIRS = frozenset(
    [
        "bin",
        "sbin",
        "usr",
        "etc",
        "var",
        "lib",
        "opt",
        "system",
        "library",
        "applications",
        "users",
        "home",
        "private",
        "volumes",
        "dev",
        "root",
        "boot",
        "srv",
    ]
)
# The programs whose arguments are inspected. Everything else is judged by its name alone, which
# keeps a long run of wrappers (`eval eval eval ...`) from costing a pass over the rest per word.
_INSPECTS_ARGUMENTS = frozenset({"rm", "diskutil", "find"})

_DISK_WRITE = re.compile(
    r"\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b|tty\b|fd/)"
    r"|>\s*/dev/(?:sd|hd|nvme|disk|rdisk|mmcblk|vd|xvd|loop|mapper|dm-)"
)
# `:(){ :|:& };:` and kin. Bounded gaps and a left anchor keep the scan linear: the commands arrive
# from a model (or from text a model has read), and an unbounded `[^}]*` tried at every `f(){` is
# quadratic.
_FORK_BOMB = re.compile(
    r"(?<![\w:.-])([\w:.-]{1,40})\s*\(\s*\)\s*\{[^}]{0,300}\1\s*\|\s*\1[^}]{0,300}&[^}]{0,300}\}"
)
_ASSIGNMENT = re.compile(r"[a-z_][a-z0-9_]*=")
_NUMBER = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_SHELL_C = re.compile(r"-[a-z]*c")
# Programs that run another program, so that what follows them is a command too.
WRAPPERS = frozenset(
    [
        "env",
        "command",
        "exec",
        "nohup",
        "time",
        "nice",
        "ionice",
        "xargs",
        "builtin",
        "eval",
        "timeout",
        "stdbuf",
        "then",
        "do",
        "else",
        "if",
        "while",
        "until",
        "!",
        "{",
    ]
)
SHELL_NAMES = frozenset(["sh", "bash", "zsh", "dash", "ksh", "fish"])


def normalize(command: str, *, fold: bool = True) -> str:
    """The command as it will behave, not as it is written: lowercased (unless `fold` is false),
    quotes and backslashes removed, `${IFS}` turned into the space it stands for."""
    text = (command.lower() if fold else command).replace("\\\n", " ")
    text = re.sub(r"(?i)\$\{?ifs\}?", " ", text)
    text = re.sub(r"[\\'\"]", "", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _segments(text: str) -> list[list[str]]:
    """Simple commands as lists of words (split at ; & | newlines, subshells and backticks)."""
    return [parts.split() for parts in re.split(r"[;&|\n`()]+", text) if parts.strip()]


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def command_positions(words: list[str]) -> list[int]:
    """Which words of a simple command are run as programs: the first one, what follows a wrapper
    such as `env` or `xargs`, and what follows `sh -c`. Words that are merely arguments
    (`grep reboot logs`, `git commit -m "rm -rf /"`) are not."""
    found: list[int] = []
    expect, wrapped, shell = True, False, False
    for i, word in enumerate(words):
        if expect:
            if _ASSIGNMENT.match(word) or (
                wrapped and (word.startswith("-") or _NUMBER.fullmatch(word))
            ):
                continue
            found.append(i)
            program = _base(word)
            wrapped, shell = program in WRAPPERS, program in SHELL_NAMES
            expect = wrapped
        elif shell and _SHELL_C.fullmatch(word):
            expect, wrapped, shell = True, False, False
    return found


def simple_commands(command: str) -> list[str]:
    """Every program a shell line runs, each with its arguments, as text. `cd x && env A=1 rm -rf y
    | tee z` gives `cd x`, `env A=1 rm -rf y`, `rm -rf y` and `tee z`; `sh -c 'git push'` gives
    `git push` too, and `/bin/rm -rf y` is also given as `rm -rf y`. Quotes and backslashes are taken
    away as `normalize` does, but the case stays: a rule someone wrote for `git push` is case-sensitive.

    This is what a rule *about commands* needs to be tried on. It reads the text, so a command built
    at run time is not in it: rules are a convenience over the sandbox, not a replacement for it."""
    text = normalize(command, fold=False)
    found: list[str] = []
    for words in _segments(text):
        for i in command_positions([w.lower() for w in words]):
            found.append(" ".join(words[i:]))
            if "/" in words[i]:  # `/bin/rm -rf y` is `rm -rf y`
                found.append(" ".join([_base(words[i]), *words[i + 1 :]]))
    return found


def _lexical(path: str) -> str:
    """The path as the shell reads it before touching the disk: `//etc`, `/./etc`, `/etc/.` and
    `/tmp/../etc` all name `/etc`."""
    absolute = path.startswith("/")
    kept: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part != ".." or (kept and kept[-1] != ".."):
            if part == "..":
                kept.pop()
            else:
                kept.append(part)
        elif not absolute:
            kept.append(part)  # a relative path can climb above where it started
    joined = "/".join(kept)
    return f"/{joined}" if absolute else joined or "."


def _sensitive_target(word: str, homes: tuple[str, ...]) -> bool:
    """Whether `word` names something a recursive delete may not be pointed at, however it is
    spelled: it is judged as written and with its `.` and `..` worked out."""
    return _sensitive(word, homes) or _sensitive(_lexical(word), homes)


def _sensitive(word: str, homes: tuple[str, ...]) -> bool:
    """The root, a home directory (or one of its direct children), a top-level system directory,
    or the parent of the working directory."""
    path = word
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    for glob in ("/.*", "/*"):
        if path.endswith(glob):
            path = path[: -len(glob)]
            break
    if path in ("", "/") or re.fullmatch(r"(?:\.\./?)+\*?", path) or path == "..":
        return True
    for home in ("~", "$home", "${home}", *homes):
        if path == home:
            return True
        if path.startswith(home + "/") and "/" not in path[len(home) + 1 :]:
            return True
    if re.fullmatch(r"/(?:users|home)/[^/]+", path):
        return True
    top = re.fullmatch(r"/([^/]+)", path)
    return bool(top and top.group(1) in TOP_LEVEL_DIRS)


def _recursive(flags: list[str]) -> bool:
    return any(
        f == "--recursive" or (f.startswith("-") and not f.startswith("--") and "r" in f)
        for f in flags
    )


def _check_segment(words: list[str], homes: tuple[str, ...]) -> str | None:
    for i in command_positions(words):
        program = _base(words[i])
        if program in FORBIDDEN_PROGRAMS:
            return f"{FORBIDDEN_PROGRAMS[program]} ({program})"
        for prefix, why in FORBIDDEN_PREFIXES.items():
            if program.startswith(prefix):
                return f"{why} ({program})"
        if program not in _INSPECTS_ARGUMENTS:
            continue
        rest = words[i + 1 :]
        if program == "diskutil" and any(w.startswith(DISKUTIL_VERBS) for w in rest):
            return "erasing or repartitioning a disk (diskutil)"

        flags = [w for w in rest if w.startswith("-")]
        targets = [w for w in rest if not w.startswith("-")]
        if program == "rm" and "--no-preserve-root" in flags:
            return "deleting without the root-directory safeguard (rm --no-preserve-root)"
        if (
            program == "rm"
            and _recursive(flags)
            and any(_sensitive_target(t, homes) for t in targets)
        ):
            return "recursive rm on the root, a home directory or a system directory"
        if program == "find" and any(w in ("-delete", "-exec", "-execdir") for w in rest):
            roots = []
            for w in rest:
                if w.startswith(("-", "(", "!")):
                    break
                roots.append(w)
            if any(_sensitive_target(t, homes) for t in roots):
                return "mass deletion across the root, a home directory or a system directory"
    return None


def check_command(command: str) -> str | None:
    """Why this shell command must never run, or None when it may go on to the policy."""
    text = normalize(command)
    if not text:
        return None

    homes = tuple({str(Path.home()).lower(), os.path.expanduser("~").lower()})
    reason: str | None = None
    for words in _segments(text):
        reason = _check_segment(words, homes)
        if reason:
            break

    if reason is None:
        checks = (
            (_FORK_BOMB.search, "a fork bomb"),
            (_DISK_WRITE.search, "writing directly to a disk device"),
        )
        reason = next((why for found, why in checks if found(text)), None)

    return f"{reason}{DENIED_SUFFIX}" if reason else None
