"""Episode/RAW evidence scoring shared by runtime and retrieval evaluation.

Hybrid retrieval finds a reasonably broad card pool from compact card text.
This module verifies only that pool against the less lossy Episode/RAW text,
then combines the two ranks.  Embedding cache rows contain vectors and hashes
only; conversation text is never written to the cache.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Optional

import numpy as np

from butly_core.core.gatekeeper.raw_reference import resolve_raw_reference
from butly_core.llm.embedding_profiles import DOCUMENT, QUERY
from butly_core.llm.embedding_profiles import apply_prefix
from butly_core.llm.embedding_profiles import fingerprint as embedding_fingerprint
from butly_core.llm.embedding_profiles import resolve_profile
from butly_core.llm.factory import ProviderFactory
from butly_core.llm.embedding_retry import embed_with_retry, transient_embedding_status
from butly_core.trace.collector import record_llm_call, usage_metadata


logger = logging.getLogger(__name__)

DEFAULT_RAW_CHUNK_CHARS = 1800
RAW_CHUNK_OVERLAP_CHARS = 180
EVIDENCE_PREVIEW_CHARS = 600
DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT = 0.7
_CACHE_SCHEMA_VERSION = 1


class EvidenceFusionError(ValueError):
    """Raised when evidence cannot be embedded or ranked safely."""


# Backward-compatible name used by the offline evaluator.
EvidenceRerankError = EvidenceFusionError


@dataclass(frozen=True)
class EvidenceUnit:
    instance_name: str
    card_id: str
    evidence_type: str
    source_file: Optional[str]
    text: str

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _public_embedding_fingerprint(config: dict[str, Any]) -> dict[str, Any]:
    base = embedding_fingerprint(config)
    profile = resolve_profile(config)
    return {
        "connection": str(config.get("connection") or ""),
        "model_name": base.get("model_name"),
        "profile": base.get("profile"),
        "dim": base.get("dim"),
        "query_prefix": profile.query_prefix,
        "document_prefix": profile.document_prefix,
    }


def _signature(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvidenceEmbeddingCache:
    """Transactional vector cache keyed by model, prefix kind, and text hash."""

    def __init__(self, path: Path, embedding_config: dict[str, Any]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fingerprint = _public_embedding_fingerprint(embedding_config)
        self.fingerprint_hash = _signature(self.fingerprint)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_embeddings (
                cache_key TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                embedding_kind TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector_blob BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        self.hits: Counter[str] = Counter()
        self.misses: Counter[str] = Counter()
        self.writes: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()

    def close(self) -> None:
        self._conn.close()

    def _key(self, text: str, kind: str) -> tuple[str, str]:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cache_key = hashlib.sha256(
            f"{self.fingerprint_hash}\0{kind}\0{text_hash}".encode("utf-8")
        ).hexdigest()
        return cache_key, text_hash

    def get_or_embed(
        self,
        text: str,
        kind: str,
        embedder: Callable[[str, str], Any],
    ) -> np.ndarray:
        cache_key, text_hash = self._key(text, kind)
        row = self._conn.execute(
            """
            SELECT dimensions, vector_blob
            FROM evidence_embeddings
            WHERE cache_key = ? AND schema_version = ?
            """,
            (cache_key, _CACHE_SCHEMA_VERSION),
        ).fetchone()
        if row is not None:
            dimensions = int(row[0])
            vector = np.frombuffer(row[1], dtype=np.float32)
            if dimensions > 0 and vector.size == dimensions:
                self.hits[kind] += 1
                return vector.copy()
            logger.warning("Discarding corrupt evidence embedding: %s", cache_key)
            self._conn.execute(
                "DELETE FROM evidence_embeddings WHERE cache_key = ?",
                (cache_key,),
            )
            self._conn.commit()

        self.misses[kind] += 1
        try:
            raw_vector = embedder(text, kind)
            vector = np.asarray(raw_vector, dtype=np.float32).reshape(-1)
            if vector.size == 0 or not np.all(np.isfinite(vector)):
                raise EvidenceFusionError("embedding returned an invalid vector")
        except Exception:
            self.errors[kind] += 1
            raise

        self._conn.execute(
            """
            INSERT OR REPLACE INTO evidence_embeddings (
                cache_key, schema_version, fingerprint, embedding_kind,
                text_hash, dimensions, vector_blob, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                _CACHE_SCHEMA_VERSION,
                self.fingerprint_hash,
                kind,
                text_hash,
                int(vector.size),
                vector.tobytes(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        self.writes[kind] += 1
        return vector

    def diagnostics(self) -> dict[str, Any]:
        kinds = sorted(
            set(self.hits) | set(self.misses) | set(self.writes) | set(self.errors)
        )
        return {
            "path": str(self.path),
            "schema_version": _CACHE_SCHEMA_VERSION,
            "embedding": self.fingerprint,
            "fingerprint": self.fingerprint_hash,
            "hits": sum(self.hits.values()),
            "misses": sum(self.misses.values()),
            "writes": sum(self.writes.values()),
            "errors": sum(self.errors.values()),
            "by_kind": {
                kind: {
                    "hits": self.hits[kind],
                    "misses": self.misses[kind],
                    "writes": self.writes[kind],
                    "errors": self.errors[kind],
                }
                for kind in kinds
            },
        }


def default_embedder(
    embedding_config: dict[str, Any],
) -> Callable[[str, str], Any]:
    """Build an embedding function that applies query/document prefixes."""
    provider = ProviderFactory.create(embedding_config)

    def embed(text: str, kind: str) -> Any:
        prefixed = apply_prefix(text, embedding_config, kind)
        started = time.monotonic()
        error = None
        retry_diag = {}
        try:
            vector = embed_with_retry(
                lambda: provider.embed(prefixed, config=embedding_config), retry_diag
            )
            if vector is None:
                raise EvidenceFusionError(
                    "embedding provider returned no vector"
                )
            return vector
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            record_llm_call(
                purpose="evidence_embedding",
                model=str(embedding_config.get("model_name") or ""),
                connection_id=str(embedding_config.get("connection") or ""),
                duration_ms=int((time.monotonic() - started) * 1000),
                prompt_chars=len(prefixed),
                error=error,
                metadata={**(usage_metadata(provider) or {}), **retry_diag},
            )

    return embed


def parse_source_files(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    names = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def split_raw_text(text: str, max_chars: int) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    overlap = min(RAW_CHUNK_OVERLAP_CHARS, max_chars // 4)
    step = max(max_chars - overlap, 1)
    chunks = []
    for start in range(0, len(clean), step):
        chunk = clean[start:start + max_chars].strip()
        if chunk:
            chunks.append(chunk)
        if start + max_chars >= len(clean):
            break
    return chunks


def cosine(left: np.ndarray, right: np.ndarray) -> Optional[float]:
    if left.size == 0 or right.size == 0 or left.shape != right.shape:
        return None
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return None
    return float(np.dot(left, right) / denominator)


def fuse_hybrid_evidence_ranks(
    base_candidate_ids: list[str],
    evidence_scores: list[dict[str, Any]],
    *,
    base_weight: float = DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Fuse hybrid and Evidence ranks without discarding the hybrid signal."""
    if isinstance(base_weight, bool) or not 0.0 <= float(base_weight) <= 1.0:
        raise EvidenceFusionError("evidence fusion base weight must be in [0, 1]")
    evidence_ranks = {
        str(item.get("card_id")): int(item["evidence_rank"])
        for item in evidence_scores
        if isinstance(item, dict)
        and item.get("card_id") is not None
        and isinstance(item.get("evidence_rank"), int)
        and item["evidence_rank"] > 0
    }
    evidence_by_card = {
        str(item.get("card_id")): item
        for item in evidence_scores
        if isinstance(item, dict) and item.get("card_id") is not None
    }
    evidence_weight = 1.0 - float(base_weight)
    fallback_rank = len(base_candidate_ids) + 1
    rows = []
    for base_rank, raw_card_id in enumerate(base_candidate_ids, start=1):
        card_id = str(raw_card_id)
        evidence_rank = evidence_ranks.get(card_id, fallback_rank)
        fusion_score = (
            float(base_weight) / base_rank + evidence_weight / evidence_rank
        )
        evidence_item = evidence_by_card.get(card_id) or {}
        rows.append(
            {
                "card_id": card_id,
                "base_rank": base_rank,
                "evidence_rank": evidence_rank,
                "fusion_rank": None,
                "fusion_score": fusion_score,
                "evidence_score": evidence_item.get("score"),
                "evidence_type": evidence_item.get("evidence_type"),
                "source_file": evidence_item.get("source_file"),
            }
        )
    rows.sort(
        key=lambda item: (
            -item["fusion_score"],
            item["base_rank"],
            item["card_id"],
        )
    )
    for fusion_rank, item in enumerate(rows, start=1):
        item["fusion_rank"] = fusion_rank
    return [item["card_id"] for item in rows], rows


class RuntimeEvidenceFusion:
    """Lazily score Episode/RAW evidence for one runtime candidate pool."""

    def __init__(
        self,
        instances_dir: Path,
        embedding_config: dict[str, Any],
        *,
        cache_path: Path,
        raw_chunk_chars: int = DEFAULT_RAW_CHUNK_CHARS,
        locale: str = "en",
        embedder: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        if isinstance(raw_chunk_chars, bool) or not 200 <= raw_chunk_chars <= 10000:
            raise EvidenceFusionError(
                "evidence raw chunk size must be between 200 and 10000 characters"
            )
        self.instances_dir = Path(instances_dir)
        self.embedding_config = dict(embedding_config)
        self.raw_chunk_chars = int(raw_chunk_chars)
        self.locale = locale if locale in {"en", "ja"} else "en"
        self._embedder = embedder or default_embedder(self.embedding_config)
        self.cache = EvidenceEmbeddingCache(cache_path, self.embedding_config)
        self._rendering_cache: dict[str, tuple[str, str, str]] = {}

    def close(self) -> None:
        self.cache.close()

    def embed_query(self, question: str) -> np.ndarray:
        return self.cache.get_or_embed(question, QUERY, self._embedder)

    def _instance_rendering(self, instance_name: str) -> tuple[str, str, str]:
        cached = self._rendering_cache.get(instance_name)
        if cached is not None:
            return cached
        user_name = "User"
        agent_name = "AI"
        locale = self.locale
        config_path = self.instances_dir / instance_name / "config.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
            payload = {}
        if isinstance(payload, dict):
            user_profile = payload.get("user_profile") or {}
            agent_profile = payload.get("agent_profile") or {}
            if isinstance(user_profile, dict):
                configured = str(
                    user_profile.get("user_name")
                    or user_profile.get("preferred_call")
                    or ""
                ).strip()
                if configured:
                    user_name = configured
            if isinstance(agent_profile, dict):
                configured = str(agent_profile.get("ai_name") or "").strip()
                if configured:
                    agent_name = configured
                configured_locale = str(
                    agent_profile.get("locale") or ""
                ).strip()
                if configured_locale in {"en", "ja"}:
                    locale = configured_locale
        rendering = (user_name, agent_name, locale)
        self._rendering_cache[instance_name] = rendering
        return rendering

    def _candidate_units(
        self,
        candidate: dict[str, Any],
        default_instance: str,
    ) -> list[EvidenceUnit]:
        card_id = str(candidate.get("id") or "").strip()
        instance_name = str(
            candidate.get("source_instance") or default_instance
        ).strip()
        if not card_id or not instance_name:
            return []
        title = str(candidate.get("title") or "").strip()
        episode = str(candidate.get("episode") or "").strip()
        summary = str(candidate.get("summary") or "").strip()
        units: list[EvidenceUnit] = []
        if episode:
            units.append(
                EvidenceUnit(
                    instance_name,
                    card_id,
                    "episode",
                    None,
                    f"Title: {title}\nEpisode: {episode}",
                )
            )
        user_name, agent_name, locale = self._instance_rendering(instance_name)
        source_date = str(candidate.get("source_date") or "").strip()
        for source_file in parse_source_files(candidate.get("source_files")):
            raw = resolve_raw_reference(
                [
                    {
                        "source_instance": instance_name,
                        "source_date": source_date,
                        "source_files": [source_file],
                    }
                ],
                self.instances_dir,
                instance_name,
                max_chars=0,
                user_name=user_name,
                agent_name=agent_name,
                locale=locale,
            )
            if not raw:
                continue
            for chunk in split_raw_text(raw["text"], self.raw_chunk_chars):
                units.append(
                    EvidenceUnit(
                        instance_name,
                        card_id,
                        "raw",
                        source_file,
                        f"Title: {title}\nSource conversation:\n{chunk}",
                    )
                )
        if not units and (summary or title):
            units.append(
                EvidenceUnit(
                    instance_name,
                    card_id,
                    "summary_fallback",
                    None,
                    f"Title: {title}\nSummary: {summary}",
                )
            )
        return units

    def rerank(
        self,
        question: str,
        candidates: list[dict[str, Any]],
        *,
        default_instance: str,
        top_n: int,
        base_weight: float = DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
        query_vector: Optional[np.ndarray] = None,
    ) -> dict[str, Any]:
        """Score current candidates and return a hybrid/evidence fused order."""
        started = time.monotonic()
        original_ids = [str(item.get("id")) for item in candidates]
        if not candidates:
            return {
                "status": "skipped",
                "fallback": False,
                "error": None,
                "candidate_ids": [],
                "selected_candidate_ids": [],
                "evidence_candidate_ids": [],
                "scores": [],
                "fusion_scores": [],
                "candidate_count": 0,
                "scored_count": 0,
                "missing_count": 0,
                "evidence_unit_count": 0,
                "document_error_count": 0,
                "cache": {},
                "latency_ms": 0,
            }
        try:
            if query_vector is None:
                query_vector = self.embed_query(question)
            query_vector = np.asarray(query_vector, dtype=np.float32).reshape(-1)
            if query_vector.size == 0 or not np.all(np.isfinite(query_vector)):
                raise EvidenceFusionError("query embedding is invalid")
        except Exception as exc:
            return self._fallback(original_ids, top_n, started, exc)

        ranked = []
        document_errors: Counter[str] = Counter()
        evidence_unit_count = 0
        cache_before = self.cache.diagnostics()
        for original_rank, candidate in enumerate(candidates, start=1):
            units = self._candidate_units(candidate, default_instance)
            evidence_unit_count += len(units)
            best_score = None
            best_unit = None
            for unit in units:
                try:
                    vector = self.cache.get_or_embed(
                        unit.text,
                        DOCUMENT,
                        self._embedder,
                    )
                    score = cosine(query_vector, vector)
                except Exception as exc:
                    if transient_embedding_status(exc) is not None:
                        return self._fallback(original_ids, top_n, started, exc)
                    document_errors[f"{type(exc).__name__}: {exc}"[:500]] += 1
                    continue
                if score is not None and (
                    best_score is None or score > best_score
                ):
                    best_score = score
                    best_unit = unit
            ranked.append(
                {
                    "candidate": candidate,
                    "card_id": str(candidate.get("id")),
                    "original_rank": original_rank,
                    "evidence_score": best_score,
                    "unit": best_unit,
                }
            )

        with_evidence = [row for row in ranked if row["evidence_score"] is not None]
        without_evidence = [row for row in ranked if row["evidence_score"] is None]
        with_evidence.sort(
            key=lambda row: (-row["evidence_score"], row["original_rank"])
        )
        evidence_order = with_evidence + without_evidence
        scores = []
        for evidence_rank, row in enumerate(evidence_order, start=1):
            unit = row["unit"]
            scores.append(
                {
                    "card_id": row["card_id"],
                    "original_rank": row["original_rank"],
                    "evidence_rank": evidence_rank,
                    "score": row["evidence_score"],
                    "evidence_type": unit.evidence_type if unit else None,
                    "source_file": unit.source_file if unit else None,
                }
            )
        if not with_evidence:
            error = next(iter(document_errors), "no Episode or RAW evidence")
            return self._fallback(original_ids, top_n, started, error)

        fused_ids, fusion_scores = fuse_hybrid_evidence_ranks(
            original_ids,
            scores,
            base_weight=base_weight,
        )
        cache_after = self.cache.diagnostics()
        cache_delta = {
            key: int(cache_after.get(key) or 0) - int(cache_before.get(key) or 0)
            for key in ("hits", "misses", "writes", "errors")
        }
        missing_count = len(ranked) - len(with_evidence)
        status = (
            "completed"
            if missing_count == 0 and not document_errors
            else "partial"
        )
        error = next(iter(document_errors), None)
        return {
            "status": status,
            "fallback": False,
            "error": error,
            "candidate_ids": fused_ids,
            "selected_candidate_ids": fused_ids[:top_n],
            "evidence_candidate_ids": [row["card_id"] for row in evidence_order],
            "scores": scores,
            "fusion_scores": fusion_scores,
            "candidate_count": len(candidates),
            "scored_count": len(with_evidence),
            "missing_count": missing_count,
            "evidence_unit_count": evidence_unit_count,
            "document_error_count": sum(document_errors.values()),
            "cache": cache_delta,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def _fallback(
        original_ids: list[str],
        top_n: int,
        started: float,
        error: Any,
    ) -> dict[str, Any]:
        logger.warning("Evidence Fusion fallback: %s", type(error).__name__)
        return {
            "status": "fallback",
            "fallback": True,
            "error": str(error)[:500],
            "candidate_ids": original_ids,
            "selected_candidate_ids": original_ids[:top_n],
            "evidence_candidate_ids": [],
            "scores": [],
            "fusion_scores": [],
            "candidate_count": len(original_ids),
            "scored_count": 0,
            "missing_count": len(original_ids),
            "evidence_unit_count": 0,
            "document_error_count": 0,
            "cache": {},
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
