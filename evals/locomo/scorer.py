"""Official-compatible LoCoMo scoring plus Butly-specific auxiliary metrics.

Question-level rules follow the official task_eval/evaluation.py:

* answers are normalized (comma strip, lowercase, punctuation and
  ``a/an/the/and`` removal) and Porter-stemmed before token F1
* category 1 splits both sides on commas and averages, over gold parts, the
  best F1 against any predicted part
* categories 2-4 use plain token F1; category 3 grades only the part of the
  gold answer before the first semicolon
* category 5 (adversarial) scores 1 when the prediction states that no
  information is available

Exact match and answer containment are auxiliary debug metrics, not part of
the official score.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import string
from typing import Any, Optional

from .artifacts import append_jsonl, write_json
from .stemming import stem


NO_INFORMATION_PATTERNS = ("no information available", "not mentioned")
CORRECTNESS_THRESHOLD = 0.5

_ARTICLES = re.compile(r"\b(a|an|the|and)\b")
_PUNCTUATION = set(string.punctuation)


class ScoringError(ValueError):
    """Raised when a run directory lacks the artifacts needed for scoring."""


def normalize_answer(text: Any) -> str:
    value = str(text).replace(",", "").lower()
    value = "".join(char for char in value if char not in _PUNCTUATION)
    value = _ARTICLES.sub(" ", value)
    return " ".join(value.split())


def stemmed_tokens(text: Any) -> list[str]:
    return [stem(token) for token in normalize_answer(text).split()]


def token_f1(prediction: Any, ground_truth: Any) -> float:
    prediction_tokens = stemmed_tokens(prediction)
    ground_truth_tokens = stemmed_tokens(ground_truth)
    if not prediction_tokens or not ground_truth_tokens:
        return 0.0
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(prediction_tokens)
    recall = num_same / len(ground_truth_tokens)
    return (2 * precision * recall) / (precision + recall)


def exact_match(prediction: Any, ground_truth: Any) -> bool:
    return set(normalize_answer(prediction).split()) == set(
        normalize_answer(ground_truth).split()
    )


def answer_contained(prediction: Any, ground_truth: Any) -> bool:
    gold = normalize_answer(ground_truth)
    return bool(gold) and gold in normalize_answer(prediction)


def claims_no_information(prediction: Any) -> bool:
    text = str(prediction).lower()
    return any(pattern in text for pattern in NO_INFORMATION_PATTERNS)


def _multi_answer_f1(prediction: Any, ground_truth: Any) -> float:
    gold_parts = [part.strip() for part in str(ground_truth).split(",") if part.strip()]
    predicted_parts = [
        part.strip() for part in str(prediction).split(",") if part.strip()
    ]
    if not gold_parts:
        return 0.0
    if not predicted_parts:
        predicted_parts = [str(prediction)]
    return sum(
        max(token_f1(part, gold) for part in predicted_parts)
        for gold in gold_parts
    ) / len(gold_parts)


def official_score(prediction: Any, ground_truth: Any, category: int) -> float:
    if category == 5:
        return 1.0 if claims_no_information(prediction) else 0.0
    if category == 1:
        return _multi_answer_f1(prediction, ground_truth)
    if category == 3:
        ground_truth = str(ground_truth).split(";")[0].strip()
    return token_f1(prediction, ground_truth)


def score_question(prediction: Any, ground_truth: Any, category: int) -> dict:
    return {
        "official_score": official_score(prediction, ground_truth, category),
        "token_f1": token_f1(prediction, ground_truth),
        "exact_match": exact_match(prediction, ground_truth),
        "answer_contained": answer_contained(prediction, ground_truth),
        "no_information_claimed": claims_no_information(prediction),
    }


def score_run(run_dir: Path, dataset_path: Optional[Path] = None) -> dict:
    """Score a completed run and write scores.json / errors.jsonl.

    ``dataset_path`` is accepted for backward compatibility but no longer
    used: evidence coverage is provenance-based (retrieved card ids →
    ``knowledge_cards.source_files`` → saved-turn ``locomo_dialog_ids``)
    and reads the run's own workspace instead of the dataset.
    """
    run_path = Path(run_dir)
    qa_rows = _read_qa_results(run_path)
    sleeptime_rows = _read_jsonl_optional(
        run_path / "results" / "sleeptime_log.jsonl"
    )
    replay_rows = _read_jsonl_optional(run_path / "results" / "replay_log.jsonl")

    provenance = _load_provenance(run_path, qa_rows)
    question_scores = [_score_row(row, provenance) for row in qa_rows]
    scores = {
        "schema_version": 1,
        "run_id": _run_id(run_path, qa_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_count": len(question_scores),
        "official": _official_aggregate(question_scores),
        "auxiliary": _auxiliary_aggregate(question_scores),
        "butly": _butly_aggregate(question_scores, sleeptime_rows),
        "questions": question_scores,
    }
    write_json(run_path / "scores.json", scores)
    _write_errors(run_path, replay_rows, sleeptime_rows, qa_rows)
    return scores


def _read_qa_results(run_path: Path) -> list[dict]:
    qa_path = run_path / "results" / "qa_results.jsonl"
    if not qa_path.is_file():
        raise ScoringError(f"qa_results.jsonl not found under {run_path}")
    rows = _read_jsonl_optional(qa_path)
    if not rows:
        raise ScoringError(f"qa_results.jsonl is empty: {qa_path}")
    # Resume can append a question twice when interrupted between the QA write
    # and the checkpoint write; the newest record wins.
    deduplicated: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.get("sample_id")), str(row.get("question_id")))
        deduplicated[key] = row
    return list(deduplicated.values())


def _read_jsonl_optional(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _run_id(run_path: Path, qa_rows: list[dict]) -> str:
    config_path = run_path / "run_config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(config, dict) and config.get("run_id"):
                return str(config["run_id"])
        except json.JSONDecodeError:
            pass
    return str(qa_rows[0].get("run_id", run_path.name))


def _score_row(row: dict, provenance: Optional[dict]) -> dict:
    prediction = row.get("prediction") or ""
    category = int(row.get("category", 0))
    graded = score_question(prediction, row.get("expected_answer", ""), category)

    diagnostics = row.get("diagnostics") or {}
    rag = diagnostics.get("rag") or {}
    rag_results = rag.get("results") or []
    retrieval = rag.get("retrieval") or {}
    raw_reference = rag.get("raw_reference") or {}
    evidence_fusion = retrieval.get("evidence_fusion") or {}
    evidence_cache = evidence_fusion.get("cache") or {}
    token_usage = diagnostics.get("token_usage") or {}
    token_usage_total = diagnostics.get("token_usage_total") or {}
    gatekeeper = diagnostics.get("gatekeeper") or {}
    entry = {
        "question_id": row.get("question_id"),
        "sample_id": row.get("sample_id"),
        "category": category,
        "question": row.get("question"),
        "expected_answer": row.get("expected_answer"),
        "prediction": prediction,
        **graded,
        "rag_triggered": bool(rag_results),
        "retrieved_card_count": len(row.get("retrieved_card_ids") or []),
        "rag_source_mode": rag.get("source_mode"),
        "raw_reference_status": raw_reference.get("status"),
        "raw_reference_chars": raw_reference.get("chars"),
        "raw_reference_truncated": raw_reference.get("truncated"),
        "raw_reference_files": raw_reference.get(
            "file_count", len(raw_reference.get("files") or []) or None
        ),
        "prompt_tokens": token_usage.get("prompt_tokens"),
        "completion_tokens": token_usage.get("completion_tokens"),
        "total_prompt_tokens": token_usage_total.get("prompt_tokens"),
        "total_completion_tokens": token_usage_total.get("completion_tokens"),
        "tier": gatekeeper.get("tier", row.get("tier")),
        "need_intent": gatekeeper.get("need_intent"),
        "retrieval_query": gatekeeper.get("retrieval_query"),
        "retrieval_query_status": gatekeeper.get("retrieval_query_status"),
        "classifier_status": gatekeeper.get("classifier_status"),
        "fallback_reason": gatekeeper.get("fallback_reason"),
        "intent_floor_applied": gatekeeper.get("intent_floor_applied"),
        "latency_ms": row.get("latency_ms"),
        "error": row.get("error"),
        # 検索の実行と注入は別物（計画書 §3.7）。rag_triggered は注入の有無。
        "search_executed": bool(retrieval.get("executed")),
        "retrieval_mode": retrieval.get("mode"),
        "retrieval_candidate_count": retrieval.get("candidate_count") or 0,
        "retrieval_latency_ms": retrieval.get("latency_ms"),
        "injection_allowed": retrieval.get("injection_allowed"),
        "injection_reason": retrieval.get("injection_reason"),
        "bm25_short_term_hit": _has_short_term_hit(retrieval),
        "reranker_status": (retrieval.get("reranker") or {}).get("status"),
        "reranker_fallback": (retrieval.get("reranker") or {}).get(
            "fallback"
        ),
        "reranker_latency_ms": (retrieval.get("reranker") or {}).get(
            "latency_ms"
        ),
        "reranker_model_name": (retrieval.get("reranker") or {}).get(
            "model_name"
        ),
        "reranker_engine": (retrieval.get("reranker") or {}).get("engine"),
        "reranker_selected_count": (retrieval.get("reranker") or {}).get(
            "selected_count"
        ),
        "evidence_fusion_status": evidence_fusion.get("status"),
        "evidence_fusion_fallback": evidence_fusion.get("fallback"),
        "evidence_fusion_latency_ms": evidence_fusion.get("latency_ms"),
        "evidence_fusion_scored_count": evidence_fusion.get("scored_count"),
        "evidence_fusion_cache_hits": evidence_cache.get("hits"),
        "evidence_fusion_cache_misses": evidence_cache.get("misses"),
    }
    entry["evidence_coverage"] = _evidence_provenance_coverage(row, provenance)
    entry.update(_retrieval_coverage(row, provenance, retrieval))
    return entry


def _has_short_term_hit(retrieval: dict) -> Optional[bool]:
    """2文字CJK語の LIKE 補助候補が候補に入ったか（日本語運用の観測用）。"""
    if not retrieval.get("executed") or "short_term_hits" not in retrieval:
        return None
    return bool(retrieval.get("short_term_hits"))


def _retrieval_coverage(
    row: dict, provenance: Optional[dict], retrieval: dict
) -> dict:
    """検索候補（注入前）の Recall@k。分母は oracle カードが存在する問。

    ``evidence_coverage`` は「注入されたカード」で測るので、注入policyの影響を
    受ける。ランキング自体の良し悪しを見るために、融合後の上位k候補で同じ
    計算をする（計画書 §3.7）。``vector_recall_at_3`` は BM25 を外した場合の
    対照で、``bm25_rescue_rate`` の計算に使う。
    """
    out: dict = {
        "oracle_available": None,
        "recall_at_1": None,
        "recall_at_3": None,
        "recall_at_20": None,
        "vector_recall_at_3": None,
        "hybrid_recall_at_3": None,
        "original_recall_at_1": None,
        "original_recall_at_3": None,
        "original_recall_at_20": None,
        "retrieval_query_recall_at_1": None,
        "retrieval_query_recall_at_3": None,
        "retrieval_query_recall_at_20": None,
    }
    if not retrieval.get("executed"):
        return out
    oracle = _oracle_available(row, provenance)
    out["oracle_available"] = oracle
    if not oracle:
        return out
    fused = (
        retrieval.get("effective_candidate_ids")
        or retrieval.get("reranked_candidate_ids")
        or retrieval.get("fused_candidate_ids")
        or []
    )
    vector_ids = retrieval.get("vector_candidate_ids") or []
    hybrid_ids = retrieval.get("hybrid_candidate_ids") or []
    original_ids = retrieval.get("original_candidate_ids") or vector_ids
    retrieval_query_ids = retrieval.get("retrieval_query_candidate_ids") or []
    retrieval_query_executed = bool(retrieval.get("retrieval_query"))
    for k in (1, 3, 20):
        out[f"recall_at_{k}"] = _coverage_for_card_ids(row, provenance, fused[:k])
        out[f"original_recall_at_{k}"] = _coverage_for_card_ids(
            row, provenance, original_ids[:k]
        )
        out[f"retrieval_query_recall_at_{k}"] = (
            _coverage_for_card_ids(row, provenance, retrieval_query_ids[:k])
            if retrieval_query_executed
            else None
        )
    out["vector_recall_at_3"] = _coverage_for_card_ids(
        row, provenance, vector_ids[:3]
    )
    out["hybrid_recall_at_3"] = (
        _coverage_for_card_ids(row, provenance, hybrid_ids[:3])
        if hybrid_ids
        else None
    )
    return out


def _oracle_available(row: dict, provenance: Optional[dict]) -> Optional[bool]:
    """evidence ターンを含むカードが1枚でも存在するか（検索で救えるか）。"""
    if provenance is None:
        return None
    instance = provenance.get(str(row.get("instance_name")))
    if instance is None:
        return None
    evidence_ids = row.get("evidence") or []
    if not evidence_ids:
        return None
    evidence_files = {
        instance["dialog_files"].get(str(evidence_id))
        for evidence_id in evidence_ids
    }
    evidence_files.discard(None)
    if not evidence_files:
        return False
    return any(
        files & evidence_files for files in instance["card_files"].values()
    )


def _official_aggregate(question_scores: list[dict]) -> dict:
    by_category: dict[str, dict] = {}
    for category in sorted({entry["category"] for entry in question_scores}):
        subset = [e for e in question_scores if e["category"] == category]
        by_category[str(category)] = {
            "score": _mean([e["official_score"] for e in subset]),
            "count": len(subset),
        }
    adversarial = [e for e in question_scores if e["category"] == 5]
    return {
        "overall": _mean([e["official_score"] for e in question_scores]),
        "by_category": by_category,
        "no_information_accuracy": (
            _mean([e["official_score"] for e in adversarial])
            if adversarial
            else None
        ),
        "stemming": "porter-original (nltk-compatible except rare extensions)",
    }


def _auxiliary_aggregate(question_scores: list[dict]) -> dict:
    return {
        "token_f1_mean": _mean([e["token_f1"] for e in question_scores]),
        "exact_match_rate": _mean(
            [float(e["exact_match"]) for e in question_scores]
        ),
        "answer_containment_rate": _mean(
            [float(e["answer_contained"]) for e in question_scores]
        ),
    }


def _butly_aggregate(
    question_scores: list[dict],
    sleeptime_rows: list[dict],
) -> dict:
    correct = [
        e for e in question_scores if e["official_score"] >= CORRECTNESS_THRESHOLD
    ]
    incorrect = [
        e for e in question_scores if e["official_score"] < CORRECTNESS_THRESHOLD
    ]
    latencies = sorted(
        e["latency_ms"] for e in question_scores if e["latency_ms"] is not None
    )
    with_evidence = [
        e for e in question_scores if e["evidence_coverage"] is not None
    ]
    metrics = {
        "correctness_threshold": CORRECTNESS_THRESHOLD,
        "rag_trigger_rate": _mean(
            [float(e["rag_triggered"]) for e in question_scores]
        ),
        "rag_trigger_rate_when_correct": _mean(
            [float(e["rag_triggered"]) for e in correct]
        ),
        "rag_trigger_rate_when_incorrect": _mean(
            [float(e["rag_triggered"]) for e in incorrect]
        ),
        "retrieved_cards_mean": _mean(
            [e["retrieved_card_count"] for e in question_scores]
        ),
        "latency_ms_mean": _mean(latencies),
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "tier_distribution": _distribution(question_scores, "tier"),
        "need_intent_distribution": _distribution(question_scores, "need_intent"),
        "classifier_fallback_rate": _mean(
            [
                float(e["classifier_status"] == "fallback")
                for e in question_scores
                if e.get("classifier_status") is not None
            ]
        ),
        "classifier_fallback_reasons": _distribution(
            question_scores, "fallback_reason"
        ),
        "intent_floor_rate": _mean(
            [
                float(bool(e["intent_floor_applied"]))
                for e in question_scores
                if e.get("intent_floor_applied") is not None
            ]
        ),
        # RAG raw reference (rag_source_mode raw/both): 注入の成否と量
        "rag_source_mode_distribution": _distribution(
            question_scores, "rag_source_mode"
        ),
        "raw_reference_status_distribution": _distribution(
            question_scores, "raw_reference_status"
        ),
        "raw_reference_chars_mean": _mean(
            [
                e["raw_reference_chars"]
                for e in question_scores
                if e.get("raw_reference_chars") is not None
            ]
        ),
        "raw_reference_truncated_rate": _mean(
            [
                float(bool(e["raw_reference_truncated"]))
                for e in question_scores
                if e.get("raw_reference_truncated") is not None
            ]
        ),
        "raw_reference_files_mean": _mean(
            [
                e["raw_reference_files"]
                for e in question_scores
                if e.get("raw_reference_files") is not None
            ]
        ),
        # API 実測トークン数（provider の usage 由来。旧 run は None）
        "prompt_tokens_mean": _mean(
            [
                e["prompt_tokens"]
                for e in question_scores
                if e.get("prompt_tokens") is not None
            ]
        ),
        "prompt_tokens_total": _sum_or_none(
            [
                e["prompt_tokens"]
                for e in question_scores
                if e.get("prompt_tokens") is not None
            ]
        ),
        "completion_tokens_total": _sum_or_none(
            [
                e["completion_tokens"]
                for e in question_scores
                if e.get("completion_tokens") is not None
            ]
        ),
        # QA ターン全体（chat + classifier + state_updater + embedding 等）
        "qa_all_calls_prompt_tokens_mean": _mean(
            [
                e["total_prompt_tokens"]
                for e in question_scores
                if e.get("total_prompt_tokens") is not None
            ]
        ),
        "qa_all_calls_prompt_tokens_total": _sum_or_none(
            [
                e["total_prompt_tokens"]
                for e in question_scores
                if e.get("total_prompt_tokens") is not None
            ]
        ),
        "qa_all_calls_completion_tokens_total": _sum_or_none(
            [
                e["total_completion_tokens"]
                for e in question_scores
                if e.get("total_completion_tokens") is not None
            ]
        ),
        # Sleeptime 側（記憶構築コスト）
        "sleeptime_prompt_tokens_total": _sum_or_none(
            [
                row["llm_prompt_tokens"]
                for row in sleeptime_rows
                if row.get("llm_prompt_tokens") is not None
            ]
        ),
        "sleeptime_completion_tokens_total": _sum_or_none(
            [
                row["llm_completion_tokens"]
                for row in sleeptime_rows
                if row.get("llm_completion_tokens") is not None
            ]
        ),
        "knowledge_cards_created": sum(
            row.get("knowledge_cards_created", 0) for row in sleeptime_rows
        ),
        # カード単位の根拠に絞れた枚数 / チャンク全体へフォールバックした枚数。
        # card 比率が上がるほど RAG の原文注入が小さくなる
        "knowledge_source_files_card": sum(
            row.get("knowledge_source_files_card", 0) for row in sleeptime_rows
        ),
        "knowledge_source_files_chunk": sum(
            row.get("knowledge_source_files_chunk", 0) for row in sleeptime_rows
        ),
        "sleeptime_failures": sum(
            1 for row in sleeptime_rows if row.get("error")
        ),
        "stage2_chunk_failures": sum(
            row.get("knowledge_chunk_failures", 0) for row in sleeptime_rows
        ),
        "stage2_chunks": sum(
            row.get("knowledge_chunks", 0) for row in sleeptime_rows
        ),
        "evidence_metric": "provenance_source_files",
        "evidence_retrieval_rate": _mean(
            [e["evidence_coverage"] for e in with_evidence]
        ),
        **_retrieval_aggregate(question_scores),
    }
    return metrics


def _retrieval_aggregate(question_scores: list[dict]) -> dict:
    """検索そのものの指標（注入判定と分離。計画書 §3.7）。

    ``rag_trigger_rate`` は歴史的に「注入されたか」を測っている。検索が
    need_intent でゲートされていた頃は実行率の proxy にもなっていたが、
    ``retrieval_execution=always`` 以降は別物なので別名で並べる。
    """
    executed = [e for e in question_scores if e.get("search_executed")]
    oracle = [e for e in question_scores if e.get("oracle_available")]
    retrieval_latencies = sorted(
        e["retrieval_latency_ms"]
        for e in question_scores
        if e.get("retrieval_latency_ms") is not None
    )
    short_term = [
        e for e in question_scores if e.get("bm25_short_term_hit") is not None
    ]
    reranker_configured = [
        e for e in question_scores if e.get("reranker_status") is not None
    ]
    reranked = [
        e
        for e in reranker_configured
        if e.get("reranker_status") != "skipped"
    ]
    reranker_latencies = sorted(
        e["reranker_latency_ms"]
        for e in reranked
        if e.get("reranker_latency_ms") is not None
    )
    evidence_fusion_configured = [
        e
        for e in question_scores
        if e.get("evidence_fusion_status") is not None
    ]
    evidence_fusion_rows = [
        e
        for e in evidence_fusion_configured
        if e.get("evidence_fusion_status") != "skipped"
    ]
    evidence_fusion_latencies = sorted(
        e["evidence_fusion_latency_ms"]
        for e in evidence_fusion_rows
        if e.get("evidence_fusion_latency_ms") is not None
    )
    evidence_cache_hits = sum(
        int(e.get("evidence_fusion_cache_hits") or 0)
        for e in evidence_fusion_rows
    )
    evidence_cache_misses = sum(
        int(e.get("evidence_fusion_cache_misses") or 0)
        for e in evidence_fusion_rows
    )
    evidence_cache_lookups = evidence_cache_hits + evidence_cache_misses
    return {
        "search_execution_rate": _mean(
            [float(bool(e.get("search_executed"))) for e in question_scores]
        ),
        "retrieval_candidate_rate": _mean(
            [
                float(bool(e.get("retrieval_candidate_count")))
                for e in question_scores
            ]
        ),
        # rag_trigger_rate と同義（同値であることを保つために両方出す）
        "memory_injection_rate": _mean(
            [float(e["rag_triggered"]) for e in question_scores]
        ),
        "retrieval_mode_distribution": _distribution(
            question_scores, "retrieval_mode"
        ),
        "retrieval_query_rate": _mean(
            [float(bool(e.get("retrieval_query"))) for e in question_scores]
        ),
        "retrieval_query_status_distribution": _distribution(
            question_scores, "retrieval_query_status"
        ),
        "injection_reason_distribution": _distribution(
            question_scores, "injection_reason"
        ),
        "oracle_available_count": len(oracle),
        "retrieval_recall_at_1": _mean(_present(oracle, "recall_at_1")),
        "retrieval_recall_at_3": _mean(_present(oracle, "recall_at_3")),
        "retrieval_recall_at_20": _mean(_present(oracle, "recall_at_20")),
        "vector_only_recall_at_3": _mean(_present(oracle, "vector_recall_at_3")),
        "dual_query_original_recall_at_3": _mean(
            _present(
                [e for e in oracle if e.get("retrieval_mode") == "dual_query"],
                "original_recall_at_3",
            )
        ),
        "dual_query_rewrite_recall_at_3": _mean(
            _present(
                [e for e in oracle if e.get("retrieval_mode") == "dual_query"],
                "retrieval_query_recall_at_3",
            )
        ),
        "dual_query_rescue_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) > (e["original_recall_at_3"] or 0))
                for e in oracle
                if e.get("retrieval_mode") == "dual_query"
                and e.get("recall_at_3") is not None
                and e.get("original_recall_at_3") is not None
            ]
        ),
        "dual_query_harm_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) < (e["original_recall_at_3"] or 0))
                for e in oracle
                if e.get("retrieval_mode") == "dual_query"
                and e.get("recall_at_3") is not None
                and e.get("original_recall_at_3") is not None
            ]
        ),
        "bm25_rescue_rate": _mean(
            [
                float(
                    (
                        (
                            e["hybrid_recall_at_3"]
                            if e.get("retrieval_mode")
                            == "hybrid_evidence_fusion"
                            else e["recall_at_3"]
                        )
                        or 0
                    )
                    > (e["vector_recall_at_3"] or 0)
                )
                for e in oracle
                if e.get("retrieval_mode")
                in {"hybrid", "hybrid_evidence_fusion"}
                and (
                    e.get("hybrid_recall_at_3") is not None
                    if e.get("retrieval_mode") == "hybrid_evidence_fusion"
                    else e.get("recall_at_3") is not None
                )
                and e["vector_recall_at_3"] is not None
            ]
        ),
        "reranker_execution_rate": _mean(
            [
                float(
                    e.get("reranker_status") is not None
                    and e.get("reranker_status") != "skipped"
                )
                for e in question_scores
            ]
        ),
        "reranker_completion_rate": _mean(
            [
                float(e.get("reranker_status") == "completed")
                for e in reranked
            ]
        ),
        "reranker_fallback_rate": _mean(
            [float(bool(e.get("reranker_fallback"))) for e in reranked]
        ),
        "reranker_rescue_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) > (e["vector_recall_at_3"] or 0))
                for e in oracle
                if e.get("reranker_status") == "completed"
                and e["recall_at_3"] is not None
                and e["vector_recall_at_3"] is not None
            ]
        ),
        "reranker_harm_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) < (e["vector_recall_at_3"] or 0))
                for e in oracle
                if e.get("reranker_status") == "completed"
                and e["recall_at_3"] is not None
                and e["vector_recall_at_3"] is not None
            ]
        ),
        "reranker_latency_ms_p50": _percentile(reranker_latencies, 50),
        "reranker_latency_ms_p95": _percentile(reranker_latencies, 95),
        "reranker_model_distribution": _distribution(
            reranked, "reranker_model_name"
        ),
        "reranker_engine_distribution": _distribution(
            reranked, "reranker_engine"
        ),
        "evidence_fusion_execution_rate": _mean(
            [
                float(
                    e.get("evidence_fusion_status") is not None
                    and e.get("evidence_fusion_status") != "skipped"
                )
                for e in question_scores
            ]
        ),
        "evidence_fusion_completion_rate": _mean(
            [
                float(e.get("evidence_fusion_status") in {"completed", "partial"})
                for e in evidence_fusion_rows
            ]
        ),
        "evidence_fusion_fallback_rate": _mean(
            [
                float(bool(e.get("evidence_fusion_fallback")))
                for e in evidence_fusion_rows
            ]
        ),
        "evidence_fusion_rescue_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) > (e["hybrid_recall_at_3"] or 0))
                for e in oracle
                if e.get("evidence_fusion_status") in {"completed", "partial"}
                and e.get("recall_at_3") is not None
                and e.get("hybrid_recall_at_3") is not None
            ]
        ),
        "evidence_fusion_harm_rate_at_3": _mean(
            [
                float((e["recall_at_3"] or 0) < (e["hybrid_recall_at_3"] or 0))
                for e in oracle
                if e.get("evidence_fusion_status") in {"completed", "partial"}
                and e.get("recall_at_3") is not None
                and e.get("hybrid_recall_at_3") is not None
            ]
        ),
        "evidence_fusion_latency_ms_p50": _percentile(
            evidence_fusion_latencies, 50
        ),
        "evidence_fusion_latency_ms_p95": _percentile(
            evidence_fusion_latencies, 95
        ),
        "evidence_fusion_cache_hit_rate": (
            evidence_cache_hits / evidence_cache_lookups
            if evidence_cache_lookups
            else None
        ),
        "evidence_fusion_scored_candidates_mean": _mean(
            _present(evidence_fusion_rows, "evidence_fusion_scored_count")
        ),
        "evidence_fusion_status_distribution": _distribution(
            evidence_fusion_configured, "evidence_fusion_status"
        ),
        "retrieval_latency_ms_p50": _percentile(retrieval_latencies, 50),
        "retrieval_latency_ms_p95": _percentile(retrieval_latencies, 95),
        "bm25_short_term_hit_rate": _mean(
            [float(bool(e["bm25_short_term_hit"])) for e in short_term]
        ),
        "search_executed_count": len(executed),
    }


def _load_provenance(run_path: Path, qa_rows: list[dict]) -> Optional[dict]:
    """Build per-instance provenance maps for evidence coverage.

    Chains ``retrieved_card_ids`` → ``knowledge_cards.source_files`` →
    saved-turn files → ``meta.locomo_dialog_ids``. ``source_files`` carries
    every file of the generation chunk (not card-specific), so the metric is
    provenance via source_files. Stage 2 now records per-card sources when the
    extraction model can identify them (falling back to the whole chunk when it
    cannot), so precision varies per card. Returns None when
    the run has no workspace (e.g. scoring a copied results directory).
    """
    instances_dir = run_path / "workspace" / "butly_core" / "instances"
    if not instances_dir.is_dir():
        return None
    provenance: dict[str, dict] = {}
    instance_names = sorted(
        {str(row.get("instance_name")) for row in qa_rows if row.get("instance_name")}
    )
    for name in instance_names:
        instance_dir = instances_dir / name
        if not instance_dir.is_dir():
            continue
        provenance[name] = {
            "card_files": _load_card_source_files(instance_dir),
            "dialog_files": _load_dialog_file_map(instance_dir),
        }
    return provenance or None


def _load_card_source_files(instance_dir: Path) -> dict[str, set]:
    db_path = next(iter(sorted(instance_dir.glob("*.db"))), None)
    if db_path is None:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, source_files FROM knowledge_cards"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    mapping: dict[str, set] = {}
    for card_id, source_files in rows:
        try:
            files = json.loads(source_files) if source_files else []
        except json.JSONDecodeError:
            files = []
        mapping[str(card_id)] = {str(name) for name in files}
    return mapping


def _load_dialog_file_map(instance_dir: Path) -> dict[str, str]:
    """Map each locomo dialog id to the saved-turn file that contains it.

    Turn files move between short_term_json and the archive folders with the
    same file name, so the bare name matches ``source_files`` entries.
    """
    mapping: dict[str, str] = {}
    for turn_file in instance_dir.rglob("*.json"):
        try:
            payload = json.loads(turn_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            meta = message.get("meta") if isinstance(message, dict) else None
            if not isinstance(meta, dict):
                continue
            for dialog_id in meta.get("locomo_dialog_ids") or []:
                mapping[str(dialog_id)] = turn_file.name
    return mapping


def _evidence_provenance_coverage(
    row: dict, provenance: Optional[dict]
) -> Optional[float]:
    """Share of evidence turns whose saved file is among the retrieved cards'
    ``source_files``. Chunk-level provenance (see ``_load_provenance``)."""
    return _coverage_for_card_ids(
        row, provenance, row.get("retrieved_card_ids") or []
    )


def _coverage_for_card_ids(
    row: dict, provenance: Optional[dict], card_ids: list
) -> Optional[float]:
    """Evidence coverage for an arbitrary card list (injected cards, top-k, ...)."""
    if provenance is None:
        return None
    instance = provenance.get(str(row.get("instance_name")))
    if instance is None:
        return None
    evidence_ids = row.get("evidence") or []
    if not evidence_ids:
        return None
    retrieved_files: set = set()
    for card_id in card_ids:
        retrieved_files |= instance["card_files"].get(str(card_id), set())
    covered = sum(
        1
        for evidence_id in evidence_ids
        if instance["dialog_files"].get(str(evidence_id)) in retrieved_files
    )
    return covered / len(evidence_ids)


def _write_errors(
    run_path: Path,
    replay_rows: list[dict],
    sleeptime_rows: list[dict],
    qa_rows: list[dict],
) -> None:
    errors_path = run_path / "errors.jsonl"
    errors_path.unlink(missing_ok=True)
    sources = (
        ("replay", replay_rows),
        ("sleeptime", sleeptime_rows),
        ("qa", qa_rows),
    )
    for source, rows in sources:
        for row in rows:
            if row.get("error") or row.get("status") == "failed":
                append_jsonl(errors_path, {"source": source, **row})


def _present(entries: list[dict], key: str) -> list:
    return [e[key] for e in entries if e.get(key) is not None]


def _mean(values: list) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _sum_or_none(values: list) -> Optional[float]:
    if not values:
        return None
    return sum(values)


def _percentile(sorted_values: list, percentile: int) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (percentile / 100) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return float(
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def _distribution(question_scores: list[dict], key: str) -> dict[str, int]:
    counts: Counter = Counter(
        str(entry.get(key)) for entry in question_scores if entry.get(key) is not None
    )
    return dict(sorted(counts.items()))
