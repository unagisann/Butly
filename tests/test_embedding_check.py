"""Tests for butly_core.core.embedding_check."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from butly_core.core.embedding_check import (
    KNOWN_EMBEDDING_DIMS,
    check_embeddings,
    log_startup_check,
    scan_instance_dims,
)


def _make_instance_db(instance_dir: Path, dim: int | None) -> None:
    """Create a knowledge_cards table with one row whose blob has ``dim`` floats.

    ``dim=None`` creates the table but inserts a NULL embedding.
    """
    instance_dir.mkdir(parents=True, exist_ok=True)
    db_path = instance_dir / "butly_memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE knowledge_cards (id INTEGER PRIMARY KEY, embedding_blob BLOB)"
    )
    if dim is None:
        conn.execute("INSERT INTO knowledge_cards (embedding_blob) VALUES (NULL)")
    else:
        blob = struct.pack(f"<{dim}f", *([0.1] * dim))
        conn.execute("INSERT INTO knowledge_cards (embedding_blob) VALUES (?)", (blob,))
    conn.commit()
    conn.close()


def test_scan_dims_empty(tmp_path):
    assert scan_instance_dims(tmp_path) == {}


def test_scan_dims_single_instance(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=768)
    assert scan_instance_dims(tmp_path) == {"alpha": 768}


def test_scan_dims_skips_null_blob(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=None)
    assert scan_instance_dims(tmp_path) == {"alpha": None}


def test_scan_dims_skips_non_directory(tmp_path):
    (tmp_path / "not_an_instance.txt").write_text("noise")
    _make_instance_db(tmp_path / "alpha", dim=1536)
    assert scan_instance_dims(tmp_path) == {"alpha": 1536}


def test_check_embeddings_consistent(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=3072)
    _make_instance_db(tmp_path / "bravo", dim=3072)
    s = check_embeddings(tmp_path, configured_model="gemini-embedding-001")
    assert s["mixed"] is False
    assert s["mismatch"] is False
    assert s["actions"] == []


def test_check_embeddings_mixed(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=1536)
    _make_instance_db(tmp_path / "bravo", dim=3072)
    s = check_embeddings(tmp_path)
    assert s["mixed"] is True
    assert len(s["actions"]) == 1
    assert "Mixed embedding dimensions" in s["actions"][0]


def test_check_embeddings_mismatch_with_configured_model(tmp_path):
    # DB has 768-dim vectors, but configured model produces 3072
    _make_instance_db(tmp_path / "alpha", dim=768)
    s = check_embeddings(tmp_path, configured_model="gemini-embedding-001")
    assert s["mismatch"] is True
    assert s["configured_model_known_dim"] == 3072
    assert "Configured embedding model" in s["actions"][0]


def test_check_embeddings_unknown_model_skips_model_check(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=768)
    s = check_embeddings(tmp_path, configured_model="completely-unknown-embed-v9")
    assert s["mismatch"] is False
    assert s["configured_model_known_dim"] is None


def test_check_embeddings_strips_models_prefix(tmp_path):
    _make_instance_db(tmp_path / "alpha", dim=3072)
    s = check_embeddings(tmp_path, configured_model="models/gemini-embedding-001")
    assert s["configured_model_known_dim"] == 3072
    assert s["mismatch"] is False


def test_log_startup_check_swallows_errors(tmp_path, capsys):
    # Pass a path that does not exist — should not raise.
    s = log_startup_check(tmp_path / "missing", configured_model="text-embedding-3-small")
    assert s["actions"] == []


def test_log_startup_check_prints_warnings(tmp_path, capsys):
    _make_instance_db(tmp_path / "alpha", dim=768)
    log_startup_check(tmp_path, configured_model="gemini-embedding-001")
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "migrate_embeddings.py" in out


def test_log_startup_check_prints_ok_when_consistent(tmp_path, capsys):
    _make_instance_db(tmp_path / "alpha", dim=3072)
    _make_instance_db(tmp_path / "bravo", dim=3072)
    log_startup_check(tmp_path, configured_model="gemini-embedding-001")
    out = capsys.readouterr().out
    assert "dimension check OK" in out


def test_known_dims_table_has_current_default():
    """Sanity check: the model in default AI_CONFIG should be listed.

    If you switch the default embedding model in butly_core/config.py and
    this fails, add the new model to KNOWN_EMBEDDING_DIMS too.
    """
    from butly_core.config import AI_CONFIG

    name = AI_CONFIG.get("embedding", {}).get("model_name", "")
    # Allowed to fail soft — the function tolerates unknown models, but it's
    # nice to keep the hint table aligned with reality.
    if name:
        # Use normalized form
        from butly_core.core.embedding_check import _normalize_model_name

        assert _normalize_model_name(name) in KNOWN_EMBEDDING_DIMS, (
            f"Default embedding model {name!r} is not in KNOWN_EMBEDDING_DIMS. "
            "Add it so the startup check can compare dimensions."
        )
