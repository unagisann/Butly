"""
retrieval_replay.py
-------------------
既存 run の記憶に対して**検索だけ**を差し替えて比較する offline replay。

LLM の回答生成を伴わないので、`rerun-qa` を回す前に「そもそも根拠カードへ
届くようになったのか」だけを安く測れる（検索改修計画 §8）。同じカード・同じ
質問に対して `search_mode` を変え、`retrieval_recall_at_k` を並べる。

    # BM25 のみ（embedding 呼び出しなし＝APIキー不要）
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes bm25

    # ベクトル込みの本比較（質問1件につき embedding 1回を呼ぶ）
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes vector hybrid --profile ./eval_runs/profiles/<id>.yaml

    # カードtop20をEpisode/RAWチャンクのEmbeddingでtop3へ再順位付け
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes vector evidence_rerank --profile ./eval_runs/profiles/<id>.yaml

    # hybrid top20を同じEpisode/RAW indexでtop3へ再順位付け／順位融合
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes hybrid hybrid_evidence_rerank hybrid_evidence_fusion \
        --profile ./eval_runs/profiles/<id>.yaml

出力は stdout のサマリと、`--out` 指定時に JSON。検索用DBは一時ディレクトリへ
複製するため、元runのinstance DB・カード・RAWは変更しない。Evidence rerank系は
再利用可能なembedding cacheを元runの`retrieval_cache/`へ追加する。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from butly_core.core import hybrid_search as hs
from butly_core.core.evidence_fusion import (
    DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
    fuse_hybrid_evidence_ranks,
)
from butly_core.io_utils import atomic_write_text
from evals.locomo import scorer as S
from evals.locomo.progress import ProgressReporter, create_console_progress

RECALL_KS = (1, 3, 20)
BM25_COLUMNS = ["id", "title", "summary", "episode", "is_archived"]
EVIDENCE_RERANK_MODES = frozenset(
    {
        "evidence_rerank",
        "hybrid_evidence_rerank",
        "hybrid_evidence_fusion",
    }
)
HYBRID_EVIDENCE_MODES = frozenset(
    {"hybrid_evidence_rerank", "hybrid_evidence_fusion"}
)


def load_questions(run_dir: Path) -> list[dict]:
    """Load questions from QA results or a QA-free retrieval manifest."""
    path = run_dir / "results" / "qa_results.jsonl"
    if path.is_file():
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    manifest_path = run_dir / "results" / "retrieval_questions.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            "neither qa_results.jsonl nor retrieval_questions.json exists "
            f"under {run_dir / 'results'}"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        raise ValueError(f"invalid retrieval question manifest: {manifest_path}")
    return rows


def instance_db(base_dir: Path, instance_name: str) -> Optional[Path]:
    instance_dir = base_dir / "butly_core" / "instances" / instance_name
    if not instance_dir.is_dir():
        return None
    return next(iter(sorted(instance_dir.glob("*.db"))), None)


def mirror_workspace(run_dir: Path, dest: Path) -> Path:
    """検索対象の DB だけを一時 workspace へ複製する。

    replay は FTS 索引を作る（＝書き込む）ので、元 run の workspace は触らない。
    複製するのは `*.db`（+ WAL/SHM）だけで、カードと embedding はそのまま付いてくる。
    """
    source = run_dir / "workspace" / "butly_core" / "instances"
    for instance_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        target = dest / "butly_core" / "instances" / instance_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for db_file in instance_dir.glob("*.db*"):
            shutil.copyfile(db_file, target / db_file.name)
    return dest


def bm25_candidate_ids(
    conn: sqlite3.Connection, question: str, limit: int, brain_conf: dict
) -> list[str]:
    spec = hs.build_fts_query(question)
    out = hs.bm25_candidates(
        conn,
        spec,
        columns=BM25_COLUMNS,
        limit=limit,
        weights=brain_conf.get("bm25_weights"),
        max_df_ratio=float(brain_conf.get("bm25_max_df_ratio", 0.5)),
        min_weak_df=int(brain_conf.get("bm25_min_weak_df", 5)),
        scan_limit=int(brain_conf.get("bm25_scan_limit", 500)),
    )
    return [str(r["id"]) for r in out["results"]]


def evaluate(
    run_dir: Path,
    modes: list[str],
    *,
    limit: int = 20,
    override_config: Optional[dict] = None,
    exclude_categories: tuple = (5,),
    progress: Optional[ProgressReporter] = None,
    query_generator: Optional[Callable[[dict], Any]] = None,
    evidence_embedder: Optional[Callable[[str, str], Any]] = None,
    evidence_cache_path: Optional[Path] = None,
    evidence_raw_chunk_chars: int = 1800,
    evidence_fusion_base_weight: float = (
        DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT
    ),
) -> dict:
    """mode ごとに Recall@k / no-hit を集計する。

    ``bm25`` mode は embedding を呼ばない。``vector`` / ``hybrid`` は
    質問1件につきembeddingが1回、``dual_query``は保存済み検索文なら2回、
    旧runならGatekeeper生成1回 + embedding 2回が走る。
    Evidence rerank系は初回にEpisode/RAW文書もembeddingし、以後は永続cacheを
    再利用する。``evidence_rerank``はvector候補、
    ``hybrid_evidence_rerank``はhybrid候補をtop 3へ並べ替える。
    ``hybrid_evidence_fusion``はhybrid順位を主軸にEvidence順位を融合する。
    """
    allowed_modes = {
        "bm25",
        "vector",
        "hybrid",
        "dual_query",
        "reranked",
        "evidence_rerank",
        "hybrid_evidence_rerank",
        "hybrid_evidence_fusion",
    }
    unknown_modes = set(modes) - allowed_modes
    if not modes or unknown_modes:
        raise ValueError(f"unsupported retrieval modes: {sorted(unknown_modes)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if "reranked" in modes and limit > 100:
        raise ValueError("reranked candidate limit must be at most 100")
    if isinstance(evidence_fusion_base_weight, bool) or not isinstance(
        evidence_fusion_base_weight, (int, float)
    ):
        raise ValueError("evidence fusion base weight must be a number")
    if not 0.0 <= float(evidence_fusion_base_weight) <= 1.0:
        raise ValueError("evidence fusion base weight must be between 0 and 1")
    rows = load_questions(run_dir)
    provenance = S._load_provenance(run_dir, rows)
    if provenance is None:
        raise ValueError(f"workspace not found under {run_dir}")

    targets = [
        row
        for row in rows
        if int(row.get("category", 0)) not in exclude_categories
        and row.get("evidence")
        and S._oracle_available(row, provenance)
    ]

    results: dict = {"run": str(run_dir), "oracle_questions": len(targets)}
    with tempfile.TemporaryDirectory(prefix="butly-retrieval-replay-") as tmp:
        workspace = mirror_workspace(run_dir, Path(tmp))
        brain = None
        if {
            "vector",
            "hybrid",
            "dual_query",
            "reranked",
            "evidence_rerank",
            "hybrid_evidence_rerank",
            "hybrid_evidence_fusion",
        } & set(modes):
            from butly_core.core.brain import ButlyBrain

            brain = ButlyBrain(workspace)
        if "dual_query" in modes and query_generator is None:
            query_generator = _build_gatekeeper_query_generator(
                override_config or {}
            )

        evidence_reranker = None
        evidence_work = 0
        try:
            if EVIDENCE_RERANK_MODES & set(modes):
                from evals.locomo.evidence_reranker import EvidenceReranker

                embedding_conf = brain._resolve_embedding_conf(
                    override_config or {}
                )
                agent_profile = (override_config or {}).get(
                    "agent_profile"
                ) or {}
                evidence_reranker = EvidenceReranker(
                    run_dir,
                    embedding_conf,
                    cache_path=evidence_cache_path,
                    raw_chunk_chars=evidence_raw_chunk_chars,
                    locale=str(agent_profile.get("locale") or "en"),
                    embedder=evidence_embedder,
                )
                evidence_work = evidence_reranker.unique_document_count

            total_work = evidence_work + len(targets) * len(modes)
            if progress is not None:
                progress.emit(
                    0.0,
                    "replay",
                    f"run={run_dir.name}; modes={','.join(modes)}; "
                    f"questions={len(targets)}; evidence={evidence_work}",
                    completed=0,
                    total=total_work,
                )
            if evidence_reranker is not None:
                def evidence_progress(
                    completed: int,
                    _total: int,
                    evidence_type: str,
                ) -> None:
                    if progress is None:
                        return
                    percent = (
                        completed / total_work * 99.0
                        if total_work
                        else 99.0
                    )
                    progress.emit(
                        percent,
                        "evidence",
                        f"embedding {evidence_type} {completed}/{evidence_work}",
                        completed=completed,
                        total=total_work,
                    )

                evidence_reranker.prepare(progress=evidence_progress)

            for mode_index, mode in enumerate(modes):
                results[mode] = _evaluate_mode(
                    mode,
                    targets,
                    workspace,
                    provenance,
                    brain,
                    limit,
                    override_config or {},
                    query_generator=query_generator,
                    evidence_reranker=evidence_reranker,
                    progress=progress,
                    work_offset=(
                        evidence_work + mode_index * len(targets)
                    ),
                    total_work=total_work,
                    evidence_fusion_base_weight=float(
                        evidence_fusion_base_weight
                    ),
                )
        finally:
            if evidence_reranker is not None:
                evidence_reranker.close()
    return results


def _evaluate_mode(
    mode: str,
    targets: list[dict],
    workspace: Path,
    provenance: dict,
    brain,
    limit: int,
    override_config: dict,
    *,
    query_generator: Optional[Callable[[dict], Any]] = None,
    evidence_reranker: Any = None,
    progress: Optional[ProgressReporter] = None,
    work_offset: int = 0,
    total_work: int = 0,
    evidence_fusion_base_weight: float = (
        DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT
    ),
) -> dict:
    coverage: dict = {k: [] for k in RECALL_KS}
    hits: dict = {k: 0 for k in RECALL_KS}
    connections: dict = {}
    reranker_attempts = 0
    reranker_completed = 0
    reranker_fallbacks = 0
    reranker_rescues = 0
    reranker_harms = 0
    reranker_latencies: list[int] = []
    reranker_prompt_tokens = 0
    reranker_completion_tokens = 0
    reranker_errors: dict[str, int] = {}
    selected_coverage_at_3: list[float] = []
    selected_hits_at_3 = 0
    original_coverage: dict = {k: [] for k in RECALL_KS}
    rewrite_coverage: dict = {k: [] for k in RECALL_KS}
    query_available = 0
    query_sources: dict[str, int] = {}
    dual_query_rescues = 0
    dual_query_harms = 0
    evidence_attempts = 0
    evidence_completed = 0
    evidence_fallbacks = 0
    evidence_rescues = 0
    evidence_harms = 0
    evidence_latencies: list[int] = []
    evidence_errors: dict[str, int] = {}
    evidence_selected_coverage: list[float] = []
    evidence_selected_hits = 0
    details: list[dict] = []
    try:
        for row_index, row in enumerate(targets, start=1):
            if progress is not None:
                completed = work_offset + row_index - 1
                percent = (
                    completed / total_work * 99.0 if total_work else 99.0
                )
                progress.emit(
                    percent,
                    "retrieval",
                    f"mode={mode}; question={row_index}/{len(targets)}; "
                    f"sample={row.get('sample_id')}; "
                    f"id={row.get('question_id')}",
                    completed=completed,
                    total=total_work,
                )
            instance_name = str(row.get("instance_name"))
            diag: dict = {}
            reranker_diag: dict = {}
            vector_ids: list[str] = []
            selected_ids: list[str] = []
            vector_recall_at_3 = None
            selected_recall_at_3 = None
            query_info: dict[str, Any] = {}
            evidence_diag: dict[str, Any] = {}
            evidence_query_vector = None
            if mode == "bm25":
                conn = connections.get(instance_name)
                if conn is None:
                    db_path = instance_db(workspace, instance_name)
                    if db_path is None:
                        continue
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    hs.ensure_fts_index(conn)
                    conn.commit()
                    connections[instance_name] = conn
                brain_conf = dict(override_config.get("brain") or {})
                candidate_ids = bm25_candidate_ids(
                    conn, row["question"], limit, brain_conf
                )
            else:
                conf = _mode_override(override_config, mode, limit=limit)
                search_kwargs = {
                    "limit": 3 if mode == "reranked" else limit,
                    "threshold": conf["memory_probe"][
                        "vector_search_threshold"
                    ],
                    "override_config": conf,
                }
                if evidence_reranker is not None and mode in {
                    "vector",
                    "hybrid",
                    "reranked",
                    *EVIDENCE_RERANK_MODES,
                }:
                    try:
                        evidence_query_vector = (
                            evidence_reranker.embed_query(
                                str(row.get("question") or "")
                            )
                        )
                    except Exception:
                        # Preserve each mode's existing provider/fallback path.
                        # A transient provider may still recover in the search
                        # call or when the evidence reranker retries below.
                        evidence_query_vector = None
                    if evidence_query_vector is not None:
                        search_kwargs["query_embedding"] = (
                            evidence_query_vector
                        )
                if mode == "dual_query":
                    if query_generator is None:  # pragma: no cover - guarded
                        raise ValueError("dual_query requires a query generator")
                    query_info = _normalize_query_generation(
                        query_generator(row)
                    )
                    search_kwargs["retrieval_query"] = query_info.get(
                        "retrieval_query"
                    )
                    source = str(query_info.get("source") or "unknown")
                    query_sources[source] = query_sources.get(source, 0) + 1
                    query_available += bool(query_info.get("retrieval_query"))
                search_output = brain.quick_vector_search_diag(
                    row["question"],
                    instance_name,
                    **search_kwargs,
                )
                diag = search_output["diagnostics"]
                candidate_ids = (
                    diag.get("effective_candidate_ids")
                    if mode == "reranked"
                    else diag.get("fused_candidate_ids")
                ) or []
                if mode == "reranked":
                    reranker_diag = diag.get("reranker") or {}
                    if reranker_diag.get("status") != "skipped":
                        reranker_attempts += 1
                    if reranker_diag.get("status") == "completed":
                        reranker_completed += 1
                    if reranker_diag.get("fallback"):
                        reranker_fallbacks += 1
                    error = str(reranker_diag.get("error") or "").strip()
                    if error:
                        reranker_errors[error[:500]] = (
                            reranker_errors.get(error[:500], 0) + 1
                        )
                    latency = reranker_diag.get("latency_ms")
                    if isinstance(latency, int):
                        reranker_latencies.append(latency)
                    usage = reranker_diag.get("token_usage") or {}
                    reranker_prompt_tokens += int(
                        usage.get("prompt_tokens") or 0
                    )
                    reranker_completion_tokens += int(
                        usage.get("completion_tokens") or 0
                    )
                    vector_ids = diag.get("vector_candidate_ids") or []
                    if (
                        reranker_diag.get("status") == "completed"
                        and "selected_candidate_ids" in reranker_diag
                    ):
                        selected_ids = list(
                            reranker_diag.get("selected_candidate_ids") or []
                        )
                    else:
                        selected_ids = list(candidate_ids[:3])
                    before = S._coverage_for_card_ids(
                        row, provenance, vector_ids[:3]
                    ) or 0.0
                    after = S._coverage_for_card_ids(
                        row, provenance, selected_ids
                    ) or 0.0
                    vector_recall_at_3 = before
                    selected_recall_at_3 = after
                    reranker_rescues += after > before
                    reranker_harms += after < before
                elif mode in EVIDENCE_RERANK_MODES:
                    if evidence_reranker is None:  # pragma: no cover - guarded
                        raise ValueError(
                            f"{mode} requires an evidence index"
                        )
                    evidence_base_candidates = list(
                        search_output.get("results") or []
                    )
                    evidence_base_ids = [
                        str(candidate.get("id"))
                        for candidate in evidence_base_candidates
                    ]
                    evidence_diag = evidence_reranker.rerank(
                        str(row.get("question") or ""),
                        evidence_base_candidates,
                        top_n=3,
                        query_vector=evidence_query_vector,
                    )
                    if mode == "hybrid_evidence_fusion":
                        evidence_only_ids = list(
                            evidence_diag.get("candidate_ids") or []
                        )
                        fused_ids, fusion_scores = (
                            fuse_hybrid_evidence_ranks(
                                evidence_base_ids,
                                list(evidence_diag.get("scores") or []),
                                base_weight=evidence_fusion_base_weight,
                            )
                        )
                        evidence_diag["evidence_candidate_ids"] = (
                            evidence_only_ids
                        )
                        evidence_diag["candidate_ids"] = fused_ids
                        evidence_diag["selected_candidate_ids"] = (
                            fused_ids[:3]
                        )
                        # Evidence-only previews may refer to cards that the
                        # fused ordering did not select. Keep fusion output
                        # truthful; rank/type/source remain in fusion_scores.
                        evidence_diag["selected_matches"] = []
                        evidence_diag["fusion_scores"] = fusion_scores
                    evidence_attempts += 1
                    if evidence_diag.get("status") in {
                        "completed",
                        "partial",
                    }:
                        evidence_completed += 1
                    if evidence_diag.get("fallback"):
                        evidence_fallbacks += 1
                    error = str(evidence_diag.get("error") or "").strip()
                    if error:
                        evidence_errors[error[:500]] = (
                            evidence_errors.get(error[:500], 0) + 1
                        )
                    latency = evidence_diag.get("latency_ms")
                    if isinstance(latency, int):
                        evidence_latencies.append(latency)
                    candidate_ids = list(
                        evidence_diag.get("candidate_ids")
                        or evidence_base_ids
                    )
                    selected_ids = list(
                        evidence_diag.get("selected_candidate_ids")
                        or candidate_ids[:3]
                    )
                    before = S._coverage_for_card_ids(
                        row, provenance, evidence_base_ids[:3]
                    ) or 0.0
                    after = S._coverage_for_card_ids(
                        row, provenance, selected_ids
                    ) or 0.0
                    evidence_base_recall_at_3 = before
                    selected_recall_at_3 = after
                    evidence_rescues += after > before
                    evidence_harms += after < before

            row_coverage = {}
            for k in RECALL_KS:
                value = S._coverage_for_card_ids(
                    row, provenance, candidate_ids[:k]
                ) or 0.0
                coverage[k].append(value)
                hits[k] += value > 0
                row_coverage[k] = value
            if mode == "dual_query":
                original_ids = diag.get("original_candidate_ids") or []
                rewrite_ids = diag.get("retrieval_query_candidate_ids") or []
                original_row_coverage = {}
                rewrite_row_coverage = {}
                for k in RECALL_KS:
                    original_value = S._coverage_for_card_ids(
                        row, provenance, original_ids[:k]
                    ) or 0.0
                    rewrite_value = S._coverage_for_card_ids(
                        row, provenance, rewrite_ids[:k]
                    ) or 0.0
                    original_coverage[k].append(original_value)
                    if query_info.get("retrieval_query"):
                        # A valid rewrite that retrieves no card is a real
                        # zero. Missing rewrites are tracked by availability
                        # and must not dilute the conditional rewrite metric.
                        rewrite_coverage[k].append(rewrite_value)
                    original_row_coverage[k] = original_value
                    rewrite_row_coverage[k] = rewrite_value
                dual_query_rescues += (
                    row_coverage[3] > original_row_coverage[3]
                )
                dual_query_harms += (
                    row_coverage[3] < original_row_coverage[3]
                )
            detail = {
                "sample_id": row.get("sample_id"),
                "question_id": row.get("question_id"),
                "question": row.get("question"),
                "instance_name": instance_name,
                "category": row.get("category"),
                "evidence": list(row.get("evidence") or []),
                "recall_at_1": row_coverage[1],
                "recall_at_3": row_coverage[3],
                "recall_at_20": row_coverage[20],
                "candidate_ids": list(candidate_ids),
            }
            if mode == "reranked":
                selected_value = S._coverage_for_card_ids(
                    row, provenance, selected_ids
                ) or 0.0
                if selected_recall_at_3 is None:
                    selected_recall_at_3 = selected_value
                selected_coverage_at_3.append(selected_value)
                selected_hits_at_3 += selected_value > 0
                detail.update(
                    {
                        "vector_candidate_ids": vector_ids,
                        "selected_candidate_ids": selected_ids,
                        "vector_recall_at_3": vector_recall_at_3,
                        "selected_recall_at_3": selected_value,
                        "reranker_delta_at_3": (
                            selected_recall_at_3 - vector_recall_at_3
                            if vector_recall_at_3 is not None
                            else None
                        ),
                        "reranker_status": reranker_diag.get("status"),
                        "reranker_fallback": bool(
                            reranker_diag.get("fallback")
                        ),
                        "reranker_latency_ms": reranker_diag.get(
                            "latency_ms"
                        ),
                        "reranker_scores": reranker_diag.get("scores") or [],
                        "error": reranker_diag.get("error"),
                    }
                )
            elif mode in EVIDENCE_RERANK_MODES:
                selected_value = S._coverage_for_card_ids(
                    row, provenance, selected_ids
                ) or 0.0
                if selected_recall_at_3 is None:
                    selected_recall_at_3 = selected_value
                evidence_selected_coverage.append(selected_value)
                evidence_selected_hits += selected_value > 0
                base_search_mode = (
                    "hybrid"
                    if mode in HYBRID_EVIDENCE_MODES
                    else "vector"
                )
                detail.update(
                    {
                        "base_search_mode": base_search_mode,
                        "base_candidate_ids": evidence_base_ids,
                        "base_recall_at_3": evidence_base_recall_at_3,
                        "selected_candidate_ids": selected_ids,
                        "selected_recall_at_3": selected_value,
                        "evidence_delta_at_3": (
                            selected_recall_at_3
                            - evidence_base_recall_at_3
                            if evidence_base_recall_at_3 is not None
                            else None
                        ),
                        "evidence_rerank_status": evidence_diag.get(
                            "status"
                        ),
                        "evidence_rerank_fallback": bool(
                            evidence_diag.get("fallback")
                        ),
                        "evidence_rerank_latency_ms": evidence_diag.get(
                            "latency_ms"
                        ),
                        "evidence_scores": evidence_diag.get("scores") or [],
                        "evidence_fusion_scores": evidence_diag.get(
                            "fusion_scores"
                        )
                        or [],
                        "evidence_candidate_ids": evidence_diag.get(
                            "evidence_candidate_ids"
                        )
                        or [],
                        "selected_evidence": evidence_diag.get(
                            "selected_matches"
                        )
                        or [],
                        "error": evidence_diag.get("error"),
                    }
                )
                if base_search_mode == "vector":
                    detail.update(
                        {
                            "vector_candidate_ids": evidence_base_ids,
                            "vector_recall_at_3": (
                                evidence_base_recall_at_3
                            ),
                        }
                    )
                else:
                    detail.update(
                        {
                            "hybrid_candidate_ids": evidence_base_ids,
                            "hybrid_recall_at_3": (
                                evidence_base_recall_at_3
                            ),
                        }
                    )
            elif mode == "dual_query":
                detail.update(
                    {
                        "retrieval_query": query_info.get("retrieval_query"),
                        "retrieval_query_source": query_info.get("source"),
                        "retrieval_query_status": query_info.get("status"),
                        "need_intent": query_info.get("need_intent"),
                        "original_candidate_ids": list(
                            diag.get("original_candidate_ids") or []
                        ),
                        "retrieval_query_candidate_ids": list(
                            diag.get("retrieval_query_candidate_ids") or []
                        ),
                        "query_fusion": diag.get("query_fusion") or {},
                        **{
                            f"original_recall_at_{k}": original_row_coverage[k]
                            for k in RECALL_KS
                        },
                        **{
                            f"retrieval_query_recall_at_{k}": (
                                rewrite_row_coverage[k]
                            )
                            for k in RECALL_KS
                        },
                    }
                )
            details.append(detail)
        if progress is not None:
            completed = work_offset + len(targets)
            percent = completed / total_work * 99.0 if total_work else 99.0
            progress.emit(
                percent,
                "retrieval",
                f"mode={mode} completed; questions={len(targets)}",
                completed=completed,
                total=total_work,
            )
    finally:
        for conn in connections.values():
            conn.close()

    total = len(targets) or 1
    result = {
        **{
            f"recall_at_{k}": sum(coverage[k]) / total for k in RECALL_KS
        },
        **{f"hit_at_{k}": hits[k] for k in RECALL_KS},
        "questions": len(targets),
        "details": details,
    }
    if mode == "reranked":
        from butly_core.core.reranker import RerankerConfig

        configured_raw = override_config.get("reranker") or {}
        normalized_config = RerankerConfig.from_mapping(configured_raw)
        configured = (
            normalized_config.public_dict() if normalized_config else {}
        )
        attempts_denominator = reranker_attempts or 1
        result["reranker"] = {
            "engine": configured.get("engine", "llm"),
            "model_name": configured.get("model_name"),
            "model_revision": configured.get("model_revision"),
            "code_revision": configured.get("code_revision"),
            "connection": configured.get("connection"),
            "candidate_limit": limit,
            "output_limit": 3,
            "batch_size": configured.get("batch_size"),
            "score_threshold": configured.get("score_threshold"),
            "device": configured.get("device"),
            "max_candidate_chars": configured.get(
                "max_candidate_chars", 1600
            ),
            "attempted": reranker_attempts,
            "execution_rate": reranker_attempts / total,
            "completed": reranker_completed,
            "completion_rate": reranker_completed / attempts_denominator,
            "fallbacks": reranker_fallbacks,
            "fallback_rate": reranker_fallbacks / attempts_denominator,
            "rescued_at_3": reranker_rescues,
            "rescue_rate_at_3": reranker_rescues / total,
            "harmed_at_3": reranker_harms,
            "harm_rate_at_3": reranker_harms / total,
            "selected_recall_at_3": (
                sum(selected_coverage_at_3) / total
            ),
            "selected_hit_at_3": selected_hits_at_3,
            "latency_ms_mean": (
                sum(reranker_latencies) / len(reranker_latencies)
                if reranker_latencies
                else None
            ),
            "latency_ms_p95": _percentile(reranker_latencies, 95),
            "prompt_tokens_total": reranker_prompt_tokens,
            "completion_tokens_total": reranker_completion_tokens,
            "error_distribution": reranker_errors,
        }
    elif mode in EVIDENCE_RERANK_MODES:
        attempts_denominator = evidence_attempts or 1
        index_diagnostics = evidence_reranker.diagnostics()
        result["evidence_reranker"] = {
            **index_diagnostics,
            "base_search_mode": (
                "hybrid"
                if mode in HYBRID_EVIDENCE_MODES
                else "vector"
            ),
            "fusion": (
                {
                    "strategy": "weighted_reciprocal_rank",
                    "base_weight": evidence_fusion_base_weight,
                    "evidence_weight": 1.0
                    - evidence_fusion_base_weight,
                    "rrf_k": 0,
                }
                if mode == "hybrid_evidence_fusion"
                else None
            ),
            "candidate_limit": limit,
            "output_limit": 3,
            "attempted": evidence_attempts,
            "execution_rate": evidence_attempts / total,
            "completed": evidence_completed,
            "completion_rate": evidence_completed / attempts_denominator,
            "fallbacks": evidence_fallbacks,
            "fallback_rate": evidence_fallbacks / attempts_denominator,
            "rescued_at_3": evidence_rescues,
            "rescue_rate_at_3": evidence_rescues / total,
            "harmed_at_3": evidence_harms,
            "harm_rate_at_3": evidence_harms / total,
            "selected_recall_at_3": (
                sum(evidence_selected_coverage) / total
            ),
            "selected_hit_at_3": evidence_selected_hits,
            "latency_ms_mean": (
                sum(evidence_latencies) / len(evidence_latencies)
                if evidence_latencies
                else None
            ),
            "latency_ms_p95": _percentile(evidence_latencies, 95),
            "error_distribution": evidence_errors,
        }
    elif mode == "dual_query":
        gatekeeper_config = override_config.get("context_classifier")
        if not isinstance(gatekeeper_config, dict) or not gatekeeper_config.get(
            "model_name"
        ):
            gatekeeper_config = override_config.get("gatekeeper") or {}
        result["query_fusion"] = {
            "gatekeeper_connection": gatekeeper_config.get("connection"),
            "gatekeeper_model_name": gatekeeper_config.get("model_name"),
            "candidate_limit_per_query": int(
                (override_config.get("brain") or {}).get(
                    "dual_query_candidates", 15
                )
            ),
            "pool_limit": int(
                (override_config.get("brain") or {}).get(
                    "dual_query_pool_limit", 25
                )
            ),
            "query_available": query_available,
            "query_available_rate": query_available / total,
            "query_source_distribution": query_sources,
            **{
                f"original_recall_at_{k}": sum(original_coverage[k]) / total
                for k in RECALL_KS
            },
            **{
                f"retrieval_query_recall_at_{k}": (
                    sum(rewrite_coverage[k]) / len(rewrite_coverage[k])
                    if rewrite_coverage[k]
                    else None
                )
                for k in RECALL_KS
            },
            "rescued_at_3": dual_query_rescues,
            "rescue_rate_at_3": dual_query_rescues / total,
            "harmed_at_3": dual_query_harms,
            "harm_rate_at_3": dual_query_harms / total,
        }
    return result


def _mode_override(
    override_config: dict, mode: str, *, limit: int = 20
) -> dict:
    conf = json.loads(json.dumps(override_config)) if override_config else {}
    if mode == "reranked":
        reranker = conf.get("reranker")
        if not isinstance(reranker, dict) or not reranker.get("model_name"):
            raise ValueError(
                "reranked mode requires a reranker model in the profile or "
                "--reranker-model-name"
            )
        reranker["enabled"] = True
        reranker["candidate_limit"] = limit
        brain = conf.setdefault("brain", {})
        brain["search_mode"] = "vector"
        brain["vector_candidates"] = limit
    elif mode in EVIDENCE_RERANK_MODES:
        conf.pop("reranker", None)
        brain = conf.setdefault("brain", {})
        brain["search_mode"] = (
            "hybrid"
            if mode in HYBRID_EVIDENCE_MODES
            else "vector"
        )
        brain["vector_candidates"] = limit
        if mode in HYBRID_EVIDENCE_MODES:
            brain["bm25_candidates"] = limit
    else:
        # Profiles from a reranked run must still provide a clean vector/hybrid
        # baseline in the same offline comparison.
        conf.pop("reranker", None)
        conf.setdefault("brain", {})["search_mode"] = mode
        if mode == "dual_query":
            brain = conf["brain"]
            brain.setdefault("dual_query_candidates", 15)
            brain.setdefault("dual_query_pool_limit", 25)
    probe = conf.setdefault("memory_probe", {})
    probe.setdefault("vector_search_threshold", 0.4)
    return conf


def _stored_retrieval_query(row: dict) -> Optional[str]:
    diagnostics = row.get("diagnostics") or {}
    gatekeeper = diagnostics.get("gatekeeper") or {}
    value = gatekeeper.get("retrieval_query")
    if isinstance(value, str) and value.strip():
        return " ".join(value.split()).strip()[:500]
    return None


def _build_gatekeeper_query_generator(
    override_config: dict,
) -> Callable[[dict], dict[str, Any]]:
    """Reuse QA-time queries, or run the source profile's Gatekeeper once."""
    from butly_core.core.gatekeeper.context_classifier import ContextClassifier

    classifier = ContextClassifier()

    def generate(row: dict) -> dict[str, Any]:
        stored = _stored_retrieval_query(row)
        if stored:
            return {
                "retrieval_query": stored,
                "source": "qa_result",
                "status": "ok",
                "need_intent": (
                    ((row.get("diagnostics") or {}).get("gatekeeper") or {}).get(
                        "need_intent"
                    )
                ),
            }
        configured = any(
            isinstance(override_config.get(role), dict)
            and override_config[role].get("model_name")
            for role in ("context_classifier", "gatekeeper")
        )
        if not configured:
            raise ValueError(
                "dual_query needs a saved retrieval_query or a profile with "
                "a Gatekeeper/context_classifier model"
            )
        result = classifier.classify(
            str(row.get("question") or ""),
            history_msgs=[],
            current_topic="",
            override_config=override_config,
            agent_name="Butly",
        )
        need_intent = result.get("need_intent")
        raw_query = result.get("retrieval_query")
        retrieval_query = (
            raw_query
            if need_intent in ("past_fact", "relationship")
            else None
        )
        status = result.get("retrieval_query_status")
        if raw_query and retrieval_query is None:
            status = "ignored_non_memory_intent"
        return {
            "retrieval_query": retrieval_query,
            "source": "gatekeeper_replay",
            "status": status,
            "need_intent": need_intent,
            "classifier_status": result.get("classifier_status"),
            "fallback_reason": result.get("fallback_reason"),
        }

    return generate


def _normalize_query_generation(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "retrieval_query": " ".join(value.split()).strip()[:500] or None,
            "source": "custom",
            "status": "ok",
        }
    if not isinstance(value, dict):
        return {"retrieval_query": None, "source": "unknown", "status": "invalid"}
    query = value.get("retrieval_query")
    normalized = (
        " ".join(query.split()).strip()[:500]
        if isinstance(query, str)
        else ""
    )
    return {**value, "retrieval_query": normalized or None}


def _percentile(values: list[int], percentile: int) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _load_profile(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    from evals.locomo.config import load_profile

    profile = load_profile(path)
    sections = dict(profile.sections)
    if profile.locale:
        sections.setdefault("agent_profile", {})["locale"] = profile.locale
    return sections


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="対象 run ディレクトリ")
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["bm25"],
        choices=[
            "bm25",
            "vector",
            "hybrid",
            "dual_query",
            "reranked",
            "evidence_rerank",
            "hybrid_evidence_rerank",
            "hybrid_evidence_fusion",
        ],
        help="比較する検索モード（bm25 は embedding を呼ばない）",
    )
    parser.add_argument("--limit", type=int, default=20, help="取得する候補数")
    parser.add_argument("--profile", type=Path, help="評価 profile YAML（embedding 設定用）")
    parser.add_argument("--reranker-model-name", help="reranked 用モデル名")
    parser.add_argument("--reranker-connection", help="reranked 用 Connection ID")
    parser.add_argument(
        "--reranker-engine",
        choices=["auto", "cross_encoder", "llm"],
        default="auto",
    )
    parser.add_argument("--reranker-batch-size", type=int, default=20)
    parser.add_argument("--reranker-score-threshold", type=float)
    parser.add_argument("--reranker-device", default="auto")
    parser.add_argument(
        "--reranker-max-output-tokens", type=int, default=2048
    )
    parser.add_argument(
        "--reranker-max-candidate-chars", type=int, default=1600
    )
    parser.add_argument(
        "--evidence-raw-chunk-chars",
        type=int,
        default=1800,
        help="Evidence rerank系で1つのRAW断片へ入れる最大文字数",
    )
    parser.add_argument(
        "--evidence-cache",
        type=Path,
        help="Episode/RAW/質問EmbeddingのSQLiteキャッシュ",
    )
    parser.add_argument(
        "--evidence-fusion-base-weight",
        type=float,
        default=DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
        help="hybrid_evidence_fusionのhybrid順位重み（0〜1）",
    )
    parser.add_argument("--out", type=Path, help="結果 JSON の書き出し先")
    parser.add_argument(
        "--job-id",
        help="Web Jobとの対応を成果物へ記録する内部識別子",
    )
    args = parser.parse_args(argv)

    override_config = _load_profile(args.profile)
    if args.reranker_model_name:
        from butly_core.core.reranker import is_cross_encoder_model

        cross_encoder = args.reranker_engine == "cross_encoder" or (
            args.reranker_engine == "auto"
            and is_cross_encoder_model(args.reranker_model_name)
        )
        reranker_config = {
            "enabled": True,
            "engine": args.reranker_engine,
            "model_name": args.reranker_model_name,
            **(
                {"connection": args.reranker_connection}
                if args.reranker_connection
                else {}
            ),
            "candidate_limit": args.limit,
            "max_candidate_chars": args.reranker_max_candidate_chars,
        }
        if cross_encoder:
            reranker_config.update(
                {
                    "batch_size": args.reranker_batch_size,
                    "score_threshold": args.reranker_score_threshold,
                    "device": args.reranker_device,
                }
            )
        else:
            reranker_config["generation_config"] = {
                "temperature": 0.0,
                "max_output_tokens": args.reranker_max_output_tokens,
            }
        override_config["reranker"] = reranker_config
    progress = create_console_progress()
    result = evaluate(
        args.run,
        args.modes,
        limit=args.limit,
        override_config=override_config,
        progress=progress,
        evidence_cache_path=args.evidence_cache,
        evidence_raw_chunk_chars=args.evidence_raw_chunk_chars,
        evidence_fusion_base_weight=args.evidence_fusion_base_weight,
    )
    from datetime import datetime, timezone

    result["status"] = "completed"
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["limit"] = args.limit
    result["modes"] = list(args.modes)
    if "hybrid_evidence_fusion" in args.modes:
        result["evidence_fusion_base_weight"] = (
            args.evidence_fusion_base_weight
        )
    if args.job_id:
        result["job_id"] = args.job_id

    print(f"run: {result['run']}")
    print(f"oracle カードがある問: {result['oracle_questions']}")
    for mode in args.modes:
        stats = result[mode]
        line = " / ".join(
            f"@{k}: {stats[f'recall_at_{k}']:.3f} (hit {stats[f'hit_at_{k}']})"
            for k in RECALL_KS
        )
        print(f"  {mode:7s} recall {line}")
        reranker = stats.get("reranker") or {}
        if reranker:
            print(
                "           reranker "
                f"completed={reranker['completed']}/{reranker['attempted']} "
                f"rescue@3={reranker['rescued_at_3']} "
                f"harm@3={reranker['harmed_at_3']} "
                f"latency p95={reranker['latency_ms_p95']}ms"
            )
        evidence = stats.get("evidence_reranker") or {}
        if evidence:
            print(
                "           evidence "
                f"completed={evidence['completed']}/{evidence['attempted']} "
                f"rescue@3={evidence['rescued_at_3']} "
                f"harm@3={evidence['harmed_at_3']} "
                f"cache hits={evidence['cache']['hits']}"
            )
    if args.out:
        progress.emit(99.5, "save", f"writing {args.out}")
        atomic_write_text(
            args.out,
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        print(f"wrote {args.out}")
    progress.emit(100.0, "complete", "Retrieval replay completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
