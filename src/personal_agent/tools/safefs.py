"""Reaching a file below a folder without ever following a symbolic link on the way.

`files.resolve_path` decides where a request points *now*, links included. Between that decision
and the moment the file is opened, something else can change the folder: the sandboxed shell runs at
the same time as the file tools when the model asks for both in one turn, and it may write in the
workspace. A link swapped in for a folder after the check would lead the open out of the workspace.

So the open does not trust the path it was given. It walks down from the folder's own descriptor,
one name at a time, each opened relative to the one before with `O_NOFOLLOW`: a name that has
become a link is an error, not a detour. What is opened last is looked at once more (`fstat`) before
anything is read from it, and it is opened without blocking, so that a pipe someone put in the
workspace is refused instead of waiting for a writer that never comes.

POSIX only, as is the rest of the sandbox layer.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Changed(OSError):
    """A name on the way is not what it was when the path was checked (a link took its place)."""


class NotRegular(OSError):
    """What was opened is not an ordinary file: a pipe, a device."""


def _is_link(fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _step(fd: int, name: str, *, create: bool) -> int:
    """The folder `name` inside the folder `fd`, as a new descriptor."""
    try:
        try:
            return os.open(name, _DIRECTORY, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o755, dir_fd=fd)
            return os.open(name, _DIRECTORY, dir_fd=fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR) and _is_link(fd, name):
            raise Changed(exc.errno, f"{name} is no longer an ordinary folder") from exc
        raise


@contextlib.contextmanager
def below(root: Path, names: Sequence[str], *, create: bool = False) -> Iterator[int]:
    """An open descriptor for the folder `names` below `root`. With `create`, missing folders are
    made on the way."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names:
            deeper = _step(fd, name, create=create)
            os.close(fd)
            fd = deeper
        yield fd
    finally:
        os.close(fd)


def open_regular(root: Path, names: Sequence[str]) -> tuple[int, os.stat_result]:
    """A descriptor to read the ordinary file `names` below `root`, and what is known about it.

    Raises FileNotFoundError, IsADirectoryError, NotRegular (a pipe or the like) or Changed (a
    link where there was none). The caller owns the descriptor."""
    if not names:
        raise IsADirectoryError(errno.EISDIR, "that is the folder itself")
    with below(root, names[:-1]) as parent:
        try:
            fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise Changed(exc.errno, f"{names[-1]} is no longer an ordinary file") from exc
            if exc.errno == errno.ENXIO:  # a socket, or a pipe nobody writes to
                raise NotRegular(exc.errno, "not an ordinary file") from exc
            raise
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            raise IsADirectoryError(errno.EISDIR, "a folder")
        if not stat.S_ISREG(info.st_mode):
            raise NotRegular(errno.EINVAL, "not an ordinary file")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def replace(root: Path, names: Sequence[str], data: bytes) -> None:
    """Make `data` the content of the file `names` below `root`, all at once: it is written beside
    the target and renamed over it, so a reader sees the old file or the new one, and a link that
    stands where the file should be is replaced, never written through. Missing folders are made.
    The file keeps the permissions it had."""
    if not names:
        raise IsADirectoryError(errno.EISDIR, "that is the folder itself")
    name = names[-1]
    with below(root, names[:-1], create=True) as parent:
        mode = 0o644
        with contextlib.suppress(FileNotFoundError):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISREG(current.st_mode):
                mode = stat.S_IMODE(current.st_mode)
        temporary = f".{name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                os.fchmod(handle.fileno(), mode)
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)
            raise
