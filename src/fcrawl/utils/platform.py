"""Small cross-platform helpers (fcrawl runs on macOS, Linux and Windows clients)."""

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def tmp_root() -> Path:
    """Directory for fcrawl's scratch state (cache, saved pages, key-pool state).

    `/tmp` on Unix so the paths documented in the skill stay literal; the system
    temp dir on Windows, where `/tmp` does not exist.
    """
    override = os.environ.get("FCRAWL_TMP_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(tempfile.gettempdir())
    return Path("/tmp")


@contextmanager
def exclusive_lock(handle: IO) -> Iterator[None]:
    """Hold an exclusive advisory lock on an open file for the block's duration."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
