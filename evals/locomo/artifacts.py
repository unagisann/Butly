"""Artifact persistence helpers for LoCoMo evaluation runs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Optional

from butly_core.io_utils import atomic_write_text


_SNAPSHOT_FILES = (
    "mid_term_digest.txt",
    "recent_snapshot.txt",
    "recent_digest_headlines.json",
    "session_state.json",
)
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,179}$")
_MAX_READABLE_ARTIFACT_LENGTH = 96


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one durable UTF-8 JSON object to a JSONL artifact."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


def snapshot_instance(instance_dir: Path, snapshot_dir: Path) -> dict[str, Any]:
    """Capture a compact, secret-free memory-state snapshot."""
    source = Path(instance_dir)
    destination = Path(snapshot_dir)
    destination.mkdir(parents=True, exist_ok=True)

    copied_files = []
    for name in _SNAPSHOT_FILES:
        source_file = source / name
        if source_file.is_file():
            shutil.copy2(source_file, destination / name)
            copied_files.append(name)

    cards = _read_knowledge_cards(source / "butly_memory.db")
    write_json(destination / "knowledge_cards.json", cards)
    manifest = {
        "copied_files": copied_files,
        "knowledge_card_count": len(cards),
        "short_term_file_count": _count_json(source / "short_term_json"),
        "integrated_file_count": _count_json(
            source / "memory_archive" / "1_integrated"
        ),
        "knowledgeized_file_count": _count_json_recursive(
            source / "memory_archive" / "2_knowledgeized"
        ),
    }
    write_json(destination / "manifest.json", manifest)
    return manifest


def copy_latest_trace(
    instance_dir: Path,
    traces_dir: Path,
    question_id: str,
    *,
    sample_id: Optional[str] = None,
) -> Optional[Path]:
    source = Path(instance_dir) / "traces" / "latest.json"
    if not source.is_file():
        return None
    destination_root = Path(traces_dir)
    if sample_id is not None:
        destination_root = destination_root / safe_artifact_name(sample_id)
    destination = destination_root / f"{safe_artifact_name(question_id)}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def count_knowledge_cards(database_path: Path) -> int:
    database = Path(database_path)
    if not database.is_file():
        return 0
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()
    return int(row[0]) if row else 0


def resolve_retrieved_card_ids(
    database_path: Path,
    rag_results: list[dict[str, Any]],
) -> list[str]:
    """Resolve IDs omitted by ChatService debug output using title/episode pairs."""
    database = Path(database_path)
    if not database.is_file() or not rag_results:
        return []
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, title, episode FROM knowledge_cards ORDER BY id"
        ).fetchall()

    available = [
        {"id": row[0], "title": row[1] or "", "episode": row[2] or ""}
        for row in rows
    ]
    resolved = []
    for result in rag_results:
        title = result.get("title", "")
        episode = result.get("episode", "")
        match_index = next(
            (
                index
                for index, card in enumerate(available)
                if card["title"] == title
                and (not episode or card["episode"] == episode)
            ),
            None,
        )
        if match_index is None:
            continue
        resolved.append(available.pop(match_index)["id"])
    return resolved


def _read_knowledge_cards(database_path: Path) -> list[dict[str, Any]]:
    database = Path(database_path)
    if not database.is_file():
        return []
    query = (
        "SELECT id, type, category, title, tags, summary, episode, "
        "created_at, updated_at FROM knowledge_cards ORDER BY id"
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query).fetchall()]


def _count_json(directory: Path) -> int:
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0


def _count_json_recursive(directory: Path) -> int:
    return len(list(directory.rglob("*.json"))) if directory.is_dir() else 0


def safe_artifact_name(value: str) -> str:
    """Return a deterministic path component without lossy-name collisions."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"Artifact name must be a non-empty string: {value!r}")
    if _SAFE_ARTIFACT_NAME.fullmatch(value):
        return value

    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    readable = readable.strip("._-")[:_MAX_READABLE_ARTIFACT_LENGTH].rstrip(
        "._-"
    )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{readable or 'item'}__{digest}"
