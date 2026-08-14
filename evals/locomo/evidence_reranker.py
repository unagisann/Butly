"""Embedding-only Episode/RAW reranking for LoCoMo retrieval replay.

The production card index remains the first-stage source. This module builds
an evaluation-only sidecar index from each card's Episode and linked RAW
conversation files, then applies MaxP passage scoring to vector or hybrid
top-N candidates. Only vectors and hashes are cached; RAW text remains in the
run workspace.
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


logger = logging.getLogger(__name__)

DEFAULT_RAW_CHUNK_CHARS = 1800
RAW_CHUNK_OVERLAP_CHARS = 180
EVIDENCE_PREVIEW_CHARS = 600
_CACHE_SCHEMA_VERSION = 1


class EvidenceRerankError(ValueError):
    """Raised when an evidence index cannot be built or queried safely."""


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
        self._conn = sqlite3.connect(self.path)
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
                raise EvidenceRerankError("embedding returned an invalid vector")
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


def _default_embedder(
    embedding_config: dict[str, Any],
) -> Callable[[str, str], Any]:
    provider = ProviderFactory.create(embedding_config)

    def embed(text: str, kind: str) -> Any:
        prefixed = apply_prefix(text, embedding_config, kind)
        vector = provider.embed(prefixed, config=embedding_config)
        if vector is None:
            raise EvidenceRerankError("embedding provider returned no vector")
        return vector

    return embed


def _parse_source_files(value: Any) -> list[str]:
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


def _split_raw_text(text: str, max_chars: int) -> list[str]:
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


def _cosine(left: np.ndarray, right: np.ndarray) -> Optional[float]:
    if left.size == 0 or right.size == 0 or left.shape != right.shape:
        return None
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return None
    return float(np.dot(left, right) / denominator)


class EvidenceReranker:
    """Precompute evidence vectors and rerank a retrieval candidate pool."""

    def __init__(
        self,
        run_dir: Path,
        embedding_config: dict[str, Any],
        *,
        cache_path: Optional[Path] = None,
        raw_chunk_chars: int = DEFAULT_RAW_CHUNK_CHARS,
        locale: str = "en",
        embedder: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        if isinstance(raw_chunk_chars, bool) or raw_chunk_chars < 200:
            raise EvidenceRerankError(
                "evidence raw chunk size must be at least 200 characters"
            )
        self.run_dir = Path(run_dir)
        self.instances_dir = (
            self.run_dir / "workspace" / "butly_core" / "instances"
        )
        if not self.instances_dir.is_dir():
            raise EvidenceRerankError(
                f"workspace instances not found: {self.instances_dir}"
            )
        self.raw_chunk_chars = int(raw_chunk_chars)
        self.locale = locale if locale in {"en", "ja"} else "en"
        self.embedding_config = dict(embedding_config)
        self._embedder = embedder or _default_embedder(self.embedding_config)
        self.cache = EvidenceEmbeddingCache(
            cache_path
            or self.run_dir
            / "retrieval_cache"
            / "evidence_embeddings.sqlite3",
            self.embedding_config,
        )
        self._units: dict[tuple[str, str], list[EvidenceUnit]] = {}
        self._vectors: dict[str, np.ndarray] = {}
        self._card_vectors: dict[tuple[str, str], np.ndarray] = {}
        self._card_similarity_cache: dict[
            tuple[str, str, str], Optional[float]
        ] = {}
        self._unit_counts: Counter[str] = Counter()
        self._prepare_errors: Counter[str] = Counter()
        self._missing_raw_files = 0
        self._card_count = 0
        self._prepared = False
        try:
            self._prepare_units()
        except Exception:
            self.cache.close()
            raise

    @property
    def unique_document_count(self) -> int:
        return len(
            {
                unit.text_hash
                for units in self._units.values()
                for unit in units
            }
        )

    def close(self) -> None:
        self.cache.close()

    def _prepare_units(self) -> None:
        for instance_dir in sorted(
            path for path in self.instances_dir.iterdir() if path.is_dir()
        ):
            user_name, agent_name, locale = self._instance_rendering(
                instance_dir
            )
            db_path = next(iter(sorted(instance_dir.glob("*.db"))), None)
            if db_path is None:
                continue
            conn = sqlite3.connect(
                f"{db_path.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            conn.row_factory = sqlite3.Row
            try:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(knowledge_cards)")
                }
                required = {"id", "title", "summary"}
                if not required.issubset(columns):
                    continue
                optional = [
                    name
                    for name in (
                        "episode",
                        "source_date",
                        "source_files",
                        "embedding_blob",
                    )
                    if name in columns
                ]
                selected = [*sorted(required), *optional]
                rows = conn.execute(
                    f"SELECT {', '.join(selected)} FROM knowledge_cards"
                ).fetchall()
            finally:
                conn.close()
            for row in rows:
                self._add_card_units(
                    instance_dir.name,
                    dict(row),
                    user_name=user_name,
                    agent_name=agent_name,
                    locale=locale,
                )

    def _instance_rendering(
        self,
        instance_dir: Path,
    ) -> tuple[str, str, str]:
        user_name = "User"
        agent_name = "AI"
        locale = self.locale
        config_path = instance_dir / "config.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return user_name, agent_name, locale
        if not isinstance(payload, dict):
            return user_name, agent_name, locale
        user_profile = payload.get("user_profile") or {}
        agent_profile = payload.get("agent_profile") or {}
        if isinstance(user_profile, dict):
            configured_user = str(
                user_profile.get("user_name")
                or user_profile.get("preferred_call")
                or ""
            ).strip()
            if configured_user:
                user_name = configured_user
        if isinstance(agent_profile, dict):
            configured_agent = str(
                agent_profile.get("ai_name") or ""
            ).strip()
            if configured_agent:
                agent_name = configured_agent
            configured_locale = str(
                agent_profile.get("locale") or ""
            ).strip()
            if configured_locale in {"en", "ja"}:
                locale = configured_locale
        return user_name, agent_name, locale

    def _add_card_units(
        self,
        instance_name: str,
        card: dict[str, Any],
        *,
        user_name: str,
        agent_name: str,
        locale: str,
    ) -> None:
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            return
        self._card_count += 1
        raw_card_vector = card.get("embedding_blob")
        if isinstance(raw_card_vector, (bytes, bytearray, memoryview)):
            card_vector = np.frombuffer(
                bytes(raw_card_vector),
                dtype=np.float32,
            )
            if card_vector.size and np.all(np.isfinite(card_vector)):
                self._card_vectors[(instance_name, card_id)] = (
                    card_vector.copy()
                )
        title = str(card.get("title") or "").strip()
        episode = str(card.get("episode") or "").strip()
        summary = str(card.get("summary") or "").strip()
        units: list[EvidenceUnit] = []
        if episode:
            units.append(
                EvidenceUnit(
                    instance_name=instance_name,
                    card_id=card_id,
                    evidence_type="episode",
                    source_file=None,
                    text=f"Title: {title}\nEpisode: {episode}",
                )
            )

        source_date = str(card.get("source_date") or "").strip()
        for source_file in _parse_source_files(card.get("source_files")):
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
                self._missing_raw_files += 1
                continue
            for chunk in _split_raw_text(raw["text"], self.raw_chunk_chars):
                units.append(
                    EvidenceUnit(
                        instance_name=instance_name,
                        card_id=card_id,
                        evidence_type="raw",
                        source_file=source_file,
                        text=f"Title: {title}\nSource conversation:\n{chunk}",
                    )
                )

        if not units and (summary or title):
            units.append(
                EvidenceUnit(
                    instance_name=instance_name,
                    card_id=card_id,
                    evidence_type="summary_fallback",
                    source_file=None,
                    text=f"Title: {title}\nSummary: {summary}",
                )
            )
        if not units:
            return
        self._units[(instance_name, card_id)] = units
        self._unit_counts.update(unit.evidence_type for unit in units)

    def prepare(
        self,
        progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        unique: dict[str, EvidenceUnit] = {}
        for units in self._units.values():
            for unit in units:
                unique.setdefault(unit.text_hash, unit)
        if not unique:
            raise EvidenceRerankError("no Episode or RAW evidence is available")

        self._prepare_errors.clear()
        total = len(unique)
        for index, unit in enumerate(unique.values(), start=1):
            try:
                self._vectors[unit.text_hash] = self.cache.get_or_embed(
                    unit.text,
                    DOCUMENT,
                    self._embedder,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:500]
                self._prepare_errors[message] += 1
                logger.warning(
                    "Evidence embedding failed for %s/%s: %s",
                    unit.instance_name,
                    unit.card_id,
                    message,
                )
            if progress is not None:
                progress(index, total, unit.evidence_type)
        if not self._vectors:
            first_error = next(
                iter(self._prepare_errors),
                "unknown embedding error",
            )
            raise EvidenceRerankError(
                f"all Episode/RAW embeddings failed: {first_error}"
            )
        self._prepared = True
        return self.diagnostics()

    def embed_query(self, question: str) -> np.ndarray:
        """Embed/cache one question for both retrieval stages."""
        return self.cache.get_or_embed(
            question,
            QUERY,
            self._embedder,
        )

    def card_similarity(
        self,
        instance_name: str,
        left_card_id: str,
        right_card_id: str,
    ) -> Optional[float]:
        """Return MaxP similarity using vectors already built for evidence."""
        left_id = str(left_card_id)
        right_id = str(right_card_id)
        if left_id == right_id:
            return 1.0
        first, second = sorted((left_id, right_id))
        key = (str(instance_name), first, second)
        if key in self._card_similarity_cache:
            return self._card_similarity_cache[key]

        left_card_vector = self._card_vectors.get(
            (str(instance_name), left_id)
        )
        right_card_vector = self._card_vectors.get(
            (str(instance_name), right_id)
        )
        if left_card_vector is not None and right_card_vector is not None:
            card_score = _cosine(left_card_vector, right_card_vector)
            if card_score is not None:
                self._card_similarity_cache[key] = card_score
                return card_score

        left_vectors = [
            self._vectors[unit.text_hash]
            for unit in self._units.get((str(instance_name), left_id), [])
            if unit.text_hash in self._vectors
        ]
        right_vectors = [
            self._vectors[unit.text_hash]
            for unit in self._units.get((str(instance_name), right_id), [])
            if unit.text_hash in self._vectors
        ]
        similarities = [
            score
            for left in left_vectors
            for right in right_vectors
            if (score := _cosine(left, right)) is not None
        ]
        result = max(similarities) if similarities else None
        self._card_similarity_cache[key] = result
        return result

    def rerank(
        self,
        question: str,
        candidates: list[dict[str, Any]],
        *,
        top_n: int = 3,
        query_vector: Optional[np.ndarray] = None,
    ) -> dict[str, Any]:
        if not self._prepared:
            raise EvidenceRerankError("evidence index has not been prepared")
        started = time.monotonic()
        original_ids = [str(item.get("id")) for item in candidates]
        try:
            if query_vector is None:
                query_vector = self.embed_query(question)
            query_vector = np.asarray(query_vector, dtype=np.float32).reshape(-1)
            if query_vector.size == 0 or not np.all(np.isfinite(query_vector)):
                raise EvidenceRerankError("query embedding is invalid")
        except Exception as exc:
            return {
                "status": "error",
                "fallback": True,
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "candidate_ids": original_ids,
                "selected_candidate_ids": original_ids[:top_n],
                "scores": [],
                "selected_matches": [],
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

        ranked = []
        missing = 0
        for original_rank, candidate in enumerate(candidates, start=1):
            card_id = str(candidate.get("id"))
            instance_name = str(
                candidate.get("source_instance")
                or candidate.get("instance_name")
                or ""
            )
            units = self._units.get((instance_name, card_id), [])
            best_score = None
            best_unit = None
            for unit in units:
                vector = self._vectors.get(unit.text_hash)
                if vector is None:
                    continue
                score = _cosine(query_vector, vector)
                if score is None:
                    continue
                if best_score is None or score > best_score:
                    best_score = score
                    best_unit = unit
            if best_score is None:
                missing += 1
            ranked.append(
                {
                    "candidate": candidate,
                    "card_id": card_id,
                    "original_rank": original_rank,
                    "evidence_score": best_score,
                    "unit": best_unit,
                }
            )

        with_evidence = [item for item in ranked if item["evidence_score"] is not None]
        without_evidence = [item for item in ranked if item["evidence_score"] is None]
        with_evidence.sort(
            key=lambda item: (-item["evidence_score"], item["original_rank"])
        )
        reordered = with_evidence + without_evidence
        candidate_ids = [item["card_id"] for item in reordered]
        selected = reordered[:top_n]

        scores = []
        selected_matches = []
        for new_rank, item in enumerate(reordered, start=1):
            unit = item["unit"]
            score_item = {
                "card_id": item["card_id"],
                "original_rank": item["original_rank"],
                "evidence_rank": new_rank,
                "score": item["evidence_score"],
                "evidence_type": unit.evidence_type if unit else None,
                "source_file": unit.source_file if unit else None,
            }
            scores.append(score_item)
            if item in selected:
                selected_matches.append(
                    {
                        **score_item,
                        "preview": (
                            unit.text[:EVIDENCE_PREVIEW_CHARS]
                            if unit is not None
                            else None
                        ),
                    }
                )

        status = "completed"
        if not with_evidence:
            status = "fallback"
        elif missing:
            status = "partial"
        return {
            "status": status,
            "fallback": not with_evidence,
            "error": None,
            "candidate_ids": candidate_ids,
            "selected_candidate_ids": candidate_ids[:top_n],
            "scores": scores,
            "selected_matches": selected_matches,
            "candidate_count": len(candidates),
            "scored_count": len(with_evidence),
            "missing_count": missing,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "strategy": "max_evidence_cosine",
            "card_count": self._card_count,
            "evidence_unit_count": sum(self._unit_counts.values()),
            "unique_document_count": self.unique_document_count,
            "unit_distribution": dict(self._unit_counts),
            "missing_raw_files": self._missing_raw_files,
            "document_error_distribution": dict(self._prepare_errors),
            "raw_chunk_chars": self.raw_chunk_chars,
            "cache": self.cache.diagnostics(),
        }
