"""
io_utils.py
-----------
Atomic file write helpers.

Writes go to a sibling ``<path>.tmp`` file and are then promoted with
``os.replace()`` — POSIX-atomic on the same filesystem, and atomic on Windows
since Python 3.3. The original file is therefore never partially overwritten
on crash / power loss, which protects persisted user state (Key_Memory,
glossary, session_state, instance config, ...).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` to ``path``.

    Parent directory is created if missing. The temp file is fsync'd before
    rename so the bytes are durable, not just buffered.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(p)
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        # Best-effort cleanup; never mask the original error.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(p)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
