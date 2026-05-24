"""
embedding_check.py
------------------
Startup-time consistency check for stored embedding vectors.

When the configured embedding model is swapped (e.g. Gemini → OpenAI), existing
``knowledge_cards.embedding_blob`` rows still carry the old dimension. RAG will
silently return cosine-sim ~0 against the new query vector. This module
detects that mismatch and emits a warning telling the user to run
``migrate_embeddings.py``. It never auto-migrates and never blocks startup.

The check is cheap: one ``length(embedding_blob)`` query per instance DB, no
API calls. We compare two signals:

1. **Mixed across instances** — different instances have different dims.
2. **Mismatch with configured model** — DB dim differs from the dim the
   currently-configured model is *known* to produce (best-effort lookup against
   a small hardcoded table).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

# Best-effort dimension hints. Missing entries simply skip the model-vs-DB
# check; only the cross-instance mixed-dim check still runs. Add models here
# when you start using one.
KNOWN_EMBEDDING_DIMS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # Google Gemini
    "text-embedding-004": 768,
    "gemini-embedding-001": 3072,
    "gemini-embedding-2": 3072,
    # Ollama
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "bge-m3": 1024,
}


def _normalize_model_name(name: str) -> str:
    """Strip provider prefixes the codebase sometimes carries around."""
    if not name:
        return ""
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def _read_one_blob_length(db_path: Path) -> Optional[int]:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT length(embedding_blob) FROM knowledge_cards "
                "WHERE embedding_blob IS NOT NULL LIMIT 1"
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def scan_instance_dims(instances_dir: Path) -> dict[str, Optional[int]]:
    """Return ``{instance_name: dim_or_None}`` for every instance DB found."""
    result: dict[str, Optional[int]] = {}
    if not instances_dir.exists():
        return result
    for d in sorted(instances_dir.iterdir()):
        if not d.is_dir():
            continue
        db_path = d / "butly_memory.db"
        if not db_path.exists():
            continue
        n_bytes = _read_one_blob_length(db_path)
        # embedding_blob is float32 little-endian → 4 bytes per element.
        result[d.name] = (n_bytes // 4) if n_bytes else None
    return result


def check_embeddings(
    instances_dir: Path, configured_model: Optional[str] = None
) -> dict:
    """Inspect stored embedding dims and report mismatches.

    Returns a summary dict with::

        {
          "dims": {instance: dim_or_None},
          "mixed": bool,                # different dims across instances
          "mismatch": bool,             # DB dim differs from configured model
          "configured_model_known_dim": int | None,
          "actions": [str, ...],        # human-readable migration hints
        }
    """
    dims = scan_instance_dims(instances_dir)
    populated = {k: v for k, v in dims.items() if v is not None}
    distinct = set(populated.values())
    summary: dict = {
        "dims": dims,
        "mixed": len(distinct) > 1,
        "mismatch": False,
        "configured_model_known_dim": None,
        "actions": [],
    }

    if summary["mixed"]:
        summary["actions"].append(
            "Mixed embedding dimensions across instances: "
            f"{populated}. Run `python migrate_embeddings.py --all` to unify."
        )

    if configured_model:
        known = KNOWN_EMBEDDING_DIMS.get(_normalize_model_name(configured_model))
        summary["configured_model_known_dim"] = known
        if known is not None and distinct and known not in distinct:
            summary["mismatch"] = True
            summary["actions"].append(
                f"Configured embedding model '{configured_model}' produces "
                f"{known}-dim vectors, but DB contains dim(s) {sorted(distinct)}. "
                "Run `python migrate_embeddings.py --all` after switching providers."
            )

    return summary


def log_startup_check(
    instances_dir: Path, configured_model: Optional[str] = None
) -> dict:
    """Run :func:`check_embeddings` and print warnings.

    Safe to call from FastAPI lifespan / startup hooks. Any unexpected
    exception is swallowed so it cannot block server startup.
    """
    try:
        summary = check_embeddings(instances_dir, configured_model)
    except Exception as e:  # pragma: no cover — pure safety net
        print(f"[Embedding] startup check skipped due to error: {e}")
        return {"dims": {}, "mixed": False, "mismatch": False, "actions": []}

    if summary["actions"]:
        for msg in summary["actions"]:
            print(f"[Embedding] WARNING: {msg}")
    else:
        populated = {k: v for k, v in summary["dims"].items() if v is not None}
        if populated:
            distinct = sorted(set(populated.values()))
            print(
                f"[Embedding] dimension check OK ({len(populated)} instance(s), "
                f"dim={distinct[0] if len(distinct) == 1 else distinct})"
            )

    return summary
