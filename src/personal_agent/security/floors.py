"""Safety floors: shell commands that never run, whatever the mode, the rules or the owner's approval.

This is the *cheap, early, explainable* layer. It reads the command text, so it only catches
commands that say what they do; anything built at run time (`eval "$(echo cm0gLXJmIC8= | base64 -d)"`)
walks straight past it. The real boundary is the sandbox (see `sandbox.py`): a process that cannot
read your keys or leave its workspace cannot do so however the command was assembled. Floors exist
for three reasons: the owner is never asked to approve something absurd, the model is told at once
*why* so it can choose another way, and with `sandbox.backend = "none"` they are one of the few
things left.

A floor errs on the side of blocking: quotes, backslashes and `${IFS}` are stripped before matching,
so `r"m" -rf /` and `rm${IFS}-rf${IFS}/` are seen for what they are, and a harmless command that
merely *mentions* a dangerous one is refused too. The model can rephrase; it cannot be forgiven
an erased disk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DENIED_SUFFIX = "; this is blocked for safety and cannot be approved"

# Programs that are refused whatever their arguments (matched on the program's base name).
FORBIDDEN_PROGRAMS = {
    "sudo": "privilege escalation",
    "su": "privilege escalation",
    "doas": "privilege escalation",
    "pkexec": "privilege escalation",
    "fdisk": "formatting or repartitioning a disk",
    "gdisk": "formatting or repartitioning a disk",
    "parted": "formatting or repartitioning a disk",
    "wipefs": "formatting or repartitioning a disk",
    "mke2fs": "formatting a disk",
    "shutdown": "shutting the machine down",
    "reboot": "shutting the machine down",
    "halt": "shutting the machine down",
    "poweroff": "shutting the machine down",
    "launchctl": "installing background jobs or changing system services",
    "crontab": "installing background jobs or changing system services",
    "systemctl": "installing background jobs or changing system services",
    "osascript": "controlling other applications",
    "security": "reading or changing the keychain",
    "pmset": "changing system settings or accounts",
    "networksetup": "changing system settings or accounts",
    "scutil": "changing system settings or accounts",
    "csrutil": "changing system settings or accounts",
    "spctl": "changing system settings or accounts",
    "tccutil": "changing system settings or accounts",
    "dscl": "changing system settings or accounts",
    "sysadminctl": "changing system settings or accounts",
    "visudo": "changing system settings or accounts",
    "passwd": "changing system settings or accounts",
    "chpasswd": "changing system settings or accounts",
    "pkill": "killing processes by name (use `kill` with the exact process id)",
    "killall": "killing processes by name (use `kill` with the exact process id)",
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

# Directories whose recursive deletion or permission change is never acceptable.
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
RECURSIVE_COMMANDS = frozenset({"rm", "chmod", "chown", "chgrp"})
# The programs whose arguments are inspected. Everything else is judged by its name alone, which
# keeps a long run of wrappers (`eval eval eval ...`) from costing a pass over the rest per word.
_INSPECTS_ARGUMENTS = RECURSIVE_COMMANDS | {"diskutil", "kill", "find"}

_END = r"(?=$|[\s;&|<>)`])"
_END_DIR = r"(?=$|/|[\s;&|<>)`])"
_START = r"(?:^|[\s/=:<>~])"
_SHELLS = r"(?:sh|bash|zsh|dash|ksh|fish)"
_INTERPRETERS = rf"(?:{_SHELLS}|python3?|perl|ruby|node|php)"
# An interpreter that will read the program from standard input (no script, no -c, no -m).
_READS_STDIN = (
    rf"(?:sudo\s+)?(?:[^\s|;&)`]*/)?{_INTERPRETERS}(?:\s+-[sxeul]+)*(?:\s+-)?\s*(?:$|[;&|)\n`])"
)

_ENV_FILE = re.compile(
    rf"{_START}\.env(?:\.(?!example\b|sample\b|template\b|dist\b|defaults\b)[a-z0-9_.-]+)?{_END}"
)
_SECRET_DIR = re.compile(rf"{_START}\.(?:ssh|aws|gnupg|kube|azure|docker){_END_DIR}")
_SECRET_FILE = re.compile(
    rf"{_START}(?:\.netrc|\.git-credentials|\.npmrc|\.pypirc|id_(?:rsa|dsa|ecdsa|ed25519)"
    rf"|[\w.-]*\.keychain(?:-db)?){_END}"
)
_SECRET_PATH = re.compile(
    r"\.config/(?:gcloud|gh|op)\b|/etc/(?:shadow|gshadow|sudoers|master\.passwd)"
)
_STARTUP_FILE = re.compile(
    rf"{_START}\.(?:zshrc|zprofile|zshenv|zlogin|bashrc|bash_profile|bash_login|profile){_END}"
    r"|library/launch(?:agents|daemons)|/etc/(?:profile|zshrc|zshenv|paths|crontab|cron\.)"
)
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
# "A, then B later in the same simple command" is written as two searches over one command at a time:
# `A[^;&\n]*B` as a single pattern is quadratic on `curl curl curl ...`.
_DOWNLOADER = re.compile(r"\b(?:curl|wget|fetch|nc|ncat|netcat|base64|xxd)\b")
_PIPE_INTO_INTERPRETER = re.compile(rf"\|\s*{_READS_STDIN}")
_ENDS_COMMAND = re.compile(r"[;&\n]")
_EVALUATOR = re.compile(rf"\b(?:eval|{_SHELLS})\b")
_SUBSTITUTED_DOWNLOAD = re.compile(r"(?:\$\(|`)\s*(?:curl|wget)\b")
_ENDS_STAGE = re.compile(r"[;&\n|]")
_RUN_DOWNLOAD = re.compile(
    rf"(?:^|[\s;&|(])(?:eval|source|\.|{_SHELLS})\s+(?:-\w+\s+)*(?:<\(|\$\(|`)\s*(?:curl|wget)\b"
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

    This is what a rule *about commands* needs to be tried on. It reads the text, as floors do, so
    a command built at run time is not in it: rules are a convenience over the sandbox, not a
    replacement for it."""
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
    """Whether `word` names something no recursive command may be pointed at, however it is
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
        if program == "kill" and any(w == "-1" for w in rest[1:]):
            return "signalling every process you own (kill -1)"

        flags = [w for w in rest if w.startswith("-")]
        targets = [w for w in rest if not w.startswith("-")]
        if program == "rm" and "--no-preserve-root" in flags:
            return "deleting without the root-directory safeguard (rm --no-preserve-root)"
        if (
            program in RECURSIVE_COMMANDS
            and _recursive(flags)
            and any(_sensitive_target(t, homes) for t in targets)
        ):
            return f"recursive {program} on the root, a home directory or a system directory"
        if program == "find" and any(w in ("-delete", "-exec", "-execdir") for w in rest):
            roots = []
            for w in rest:
                if w.startswith(("-", "(", "!")):
                    break
                roots.append(w)
            if any(_sensitive_target(t, homes) for t in roots):
                return "mass deletion across the root, a home directory or a system directory"
    return None


def _then_within_one_command(
    first: re.Pattern[str], then: re.Pattern[str], breaks: re.Pattern[str], text: str
) -> bool:
    """True when `then` begins after a `first` and before the next of `breaks`.

    This is `first[^breaks]*then` without the quadratic cost: the leftmost `first` of a stage is
    the best one to try, and the next match of `then` is looked for once, not once per `first`."""
    position = 0
    follow: re.Match[str] | None = None
    while start := first.search(text, position):
        stop = breaks.search(text, start.end())
        limit = stop.start() if stop else len(text)
        if follow is None or follow.start() < start.end():
            follow = then.search(text, start.end())
            if follow is None:
                return False
        if follow.start() < limit:
            return True
        if stop is None:
            return False
        position = stop.end()
    return False


def _pipes_into_interpreter(text: str) -> bool:
    """`curl ... | sh`: a download or a decoder piped into an interpreter that reads its program from stdin."""
    return _then_within_one_command(_DOWNLOADER, _PIPE_INTO_INTERPRETER, _ENDS_COMMAND, text)


def _runs_downloaded_code(text: str) -> bool:
    """`sh -c "$(curl ...)"`, `eval` of a backtick `curl`, `source <(curl ...)`."""
    return bool(_RUN_DOWNLOAD.search(text)) or _then_within_one_command(
        _EVALUATOR, _SUBSTITUTED_DOWNLOAD, _ENDS_STAGE, text
    )


def check_command(command: str, *, home: Path | None = None) -> str | None:
    """Why this shell command must never run, or None when it may proceed to the policy.

    `home` is the agent's own data directory: its secrets file and database are off limits."""
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
            (_pipes_into_interpreter, "running downloaded or decoded content as code"),
            (_runs_downloaded_code, "running downloaded content as code"),
            (_ENV_FILE.search, "access to a secrets file (.env)"),
            (_SECRET_DIR.search, "access to credential directories (.ssh, .aws, .gnupg, ...)"),
            (_SECRET_FILE.search, "access to credential files (keys, tokens, keychains)"),
            (
                _SECRET_PATH.search,
                "access to credential files (cloud tokens, system password files)",
            ),
            (_STARTUP_FILE.search, "changing shell startup files or login items (persistence)"),
        )
        reason = next((why for found, why in checks if found(text)), None)

    if reason is None and home is not None:
        base = str(home.expanduser()).lower()
        if f"{base}/.env" in text or f"{base}/state.db" in text:
            reason = "access to the agent's own secrets or database"

    return f"{reason}{DENIED_SUFFIX}" if reason else None
