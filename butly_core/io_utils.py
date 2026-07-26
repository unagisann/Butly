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
from typing import Iterable, Union

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


def _restrict_secret_file_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Some platforms/filesystems do not expose POSIX permission bits.
        pass


def _env_assignment_name(line: str) -> str | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    name, _, _ = stripped.partition("=")
    return name.strip()


def upsert_env_var(path: PathLike, name: str, value: str) -> None:
    """Add or replace one variable while preserving unrelated ``.env`` lines."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    updated: list[str] = []
    replaced = False

    for line in lines:
        if _env_assignment_name(line) == name:
            if not replaced:
                updated.append(f"{name}={value}")
                replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(f"{name}={value}")

    atomic_write_text(p, "\n".join(updated) + "\n")
    _restrict_secret_file_permissions(p)


def remove_env_vars(path: PathLike, names: Iterable[str]) -> bool:
    """Remove variables from ``.env`` and preserve comments and blank lines."""
    p = Path(path)
    if not p.exists():
        return False

    names_set = set(names)
    lines = p.read_text(encoding="utf-8").splitlines()
    updated = [
        line for line in lines if _env_assignment_name(line) not in names_set
    ]
    if len(updated) == len(lines):
        return False

    text = "\n".join(updated)
    atomic_write_text(p, text + ("\n" if text else ""))
    _restrict_secret_file_permissions(p)
    return True
