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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from butly_core.llm.embedding_profiles import fingerprint as embedding_fingerprint
from butly_core.llm.embedding_profiles import resolve_profile

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


_META_DDL = """
CREATE TABLE IF NOT EXISTS embedding_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    model_name TEXT,
    profile TEXT,
    dim INTEGER,
    updated_at TEXT
)
"""


def record_embedding_meta(db_path: Path, embedding_conf: Optional[dict]) -> None:
    """embedding_blob を書いた設定を DB に刻む（差し替え検知用）。

    書き込みのたびに 1 行を upsert する。失敗しても呼び出し側の処理は
    止めない（記憶の保存より優先されるものではない）。
    """
    fp = embedding_fingerprint(embedding_conf)
    if not fp.get("model_name"):
        return
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(_META_DDL)
            conn.execute(
                """
                INSERT INTO embedding_meta (id, model_name, profile, dim, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model_name = excluded.model_name,
                    profile = excluded.profile,
                    dim = excluded.dim,
                    updated_at = excluded.updated_at
                """,
                (
                    fp["model_name"],
                    fp["profile"],
                    fp["dim"],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
    except sqlite3.Error as e:  # pragma: no cover — 保険
        print(f"[Embedding] failed to record embedding_meta: {e}")


def read_embedding_meta(db_path: Path) -> Optional[dict]:
    """記録済みの embedding 素性を返す（無ければ None）。"""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "SELECT model_name, profile, dim FROM embedding_meta WHERE id = 1"
            )
            row = cur.fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"model_name": row[0], "profile": row[1], "dim": row[2]}


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


def scan_instance_meta(instances_dir: Path) -> dict[str, Optional[dict]]:
    """Return ``{instance_name: embedding_meta_or_None}`` for every instance DB."""
    result: dict[str, Optional[dict]] = {}
    if not instances_dir.exists():
        return result
    for d in sorted(instances_dir.iterdir()):
        if not d.is_dir():
            continue
        db_path = d / "butly_memory.db"
        if not db_path.exists():
            continue
        result[d.name] = read_embedding_meta(db_path)
    return result


def check_embeddings(
    instances_dir: Path,
    configured_model: Optional[str] = None,
    embedding_conf: Optional[dict] = None,
) -> dict:
    """Inspect stored embedding dims/profiles and report mismatches.

    Two independent signals are compared:

    * **dim** — cheap, works even for DBs written before ``embedding_meta``
      existed.
    * **profile** — catches the silent case where the dimension happens to
      match but the input convention changed (e.g. nomic without prefixes vs
      nomic with ``search_document:``). Those vectors live in different
      spaces, so RAG degrades without any visible error.

    Returns a summary dict with::

        {
          "dims": {instance: dim_or_None},
          "meta": {instance: {model_name, profile, dim} | None},
          "mixed": bool,                # different dims across instances
          "mismatch": bool,             # DB dim differs from configured model
          "profile_mismatch": bool,     # stored profile/model differs from config
          "configured_model_known_dim": int | None,
          "configured_profile": str | None,
          "actions": [str, ...],        # human-readable migration hints
        }
    """
    dims = scan_instance_dims(instances_dir)
    populated = {k: v for k, v in dims.items() if v is not None}
    distinct = set(populated.values())
    summary: dict = {
        "dims": dims,
        "meta": {},
        "mixed": len(distinct) > 1,
        "mismatch": False,
        "profile_mismatch": False,
        "configured_model_known_dim": None,
        "configured_profile": None,
        "actions": [],
    }

    if embedding_conf is None and configured_model:
        embedding_conf = {"model_name": configured_model}
    if embedding_conf:
        configured_model = configured_model or embedding_conf.get("model_name")
        want = embedding_fingerprint(embedding_conf)
        summary["configured_profile"] = want["profile"]
        summary["meta"] = scan_instance_meta(instances_dir)
        stale = {
            name: meta
            for name, meta in summary["meta"].items()
            if meta
            and (
                meta.get("model_name") != want["model_name"]
                or meta.get("profile") != want["profile"]
            )
        }
        # prefix を要求するプロファイルなのに素性が未記録 = prefix 導入前の
        # コードで書かれた可能性が高い。次元は一致するので dim チェックでは
        # 検知できず、無言で検索が劣化する。カードが入っている DB だけ疑う。
        needs_prefix = bool(
            resolve_profile(embedding_conf).document_prefix
            or resolve_profile(embedding_conf).query_prefix
        )
        if needs_prefix:
            unstamped = sorted(
                name
                for name, meta in summary["meta"].items()
                if meta is None and dims.get(name) is not None
            )
            if unstamped:
                summary["profile_mismatch"] = True
                summary["actions"].append(
                    f"Embedding profile '{want['profile']}' uses query/document "
                    "prefixes, but these instances have vectors with no recorded "
                    f"profile: {unstamped}. They were most likely written before "
                    "prefixes were applied, which puts them in a different space "
                    "than current queries. Run `python migrate_embeddings.py --all` "
                    "to re-embed."
                )

        if stale:
            summary["profile_mismatch"] = True
            detail = ", ".join(
                f"{name}: stored={m.get('model_name')}/{m.get('profile')}"
                for name, m in sorted(stale.items())
            )
            summary["actions"].append(
                "Stored embeddings were written with a different model/profile "
                f"than the current config ({want['model_name']}/{want['profile']}). "
                f"{detail}. Cosine similarity across the two is meaningless — "
                "run `python migrate_embeddings.py --all` to re-embed."
            )

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
    instances_dir: Path,
    configured_model: Optional[str] = None,
    embedding_conf: Optional[dict] = None,
) -> dict:
    """Run :func:`check_embeddings` and print warnings.

    Safe to call from FastAPI lifespan / startup hooks. Any unexpected
    exception is swallowed so it cannot block server startup.
    """
    try:
        summary = check_embeddings(instances_dir, configured_model, embedding_conf)
    except Exception as e:  # pragma: no cover — pure safety net
        print(f"[Embedding] startup check skipped due to error: {e}")
        return {
            "dims": {},
            "meta": {},
            "mixed": False,
            "mismatch": False,
            "profile_mismatch": False,
            "actions": [],
        }

    if summary["actions"]:
        for msg in summary["actions"]:
            print(f"[Embedding] WARNING: {msg}")
    else:
        populated = {k: v for k, v in summary["dims"].items() if v is not None}
        if populated:
            distinct = sorted(set(populated.values()))
            profile = summary.get("configured_profile")
            suffix = f", profile={profile}" if profile else ""
            print(
                f"[Embedding] consistency check OK ({len(populated)} instance(s), "
                f"dim={distinct[0] if len(distinct) == 1 else distinct}{suffix})"
            )

    return summary
