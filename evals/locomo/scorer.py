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
import string
from typing import Any, Optional

from .artifacts import append_jsonl, write_json
from .dataset import load_dataset
from .stemming import stem


NO_INFORMATION_PATTERNS = ("no information available", "not mentioned")
CORRECTNESS_THRESHOLD = 0.5
EVIDENCE_COVERAGE_THRESHOLD = 0.6

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
    """Score a completed run and write scores.json / errors.jsonl."""
    run_path = Path(run_dir)
    qa_rows = _read_qa_results(run_path)
    sleeptime_rows = _read_jsonl_optional(
        run_path / "results" / "sleeptime_log.jsonl"
    )
    replay_rows = _read_jsonl_optional(run_path / "results" / "replay_log.jsonl")

    evidence_texts = _load_evidence_texts(dataset_path)
    question_scores = [_score_row(row, evidence_texts) for row in qa_rows]
    scores = {
        "schema_version": 1,
        "run_id": _run_id(run_path, qa_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_count": len(question_scores),
        "official": _official_aggregate(question_scores),
        "auxiliary": _auxiliary_aggregate(question_scores),
        "butly": _butly_aggregate(question_scores, sleeptime_rows, evidence_texts),
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
    deduplicated: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("question_id"))
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


def _score_row(row: dict, evidence_texts: Optional[dict[str, str]]) -> dict:
    prediction = row.get("prediction") or ""
    category = int(row.get("category", 0))
    graded = score_question(prediction, row.get("expected_answer", ""), category)

    diagnostics = row.get("diagnostics") or {}
    rag = diagnostics.get("rag") or {}
    rag_results = rag.get("results") or []
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
        "tier": gatekeeper.get("tier", row.get("tier")),
        "need_intent": gatekeeper.get("need_intent"),
        "latency_ms": row.get("latency_ms"),
        "error": row.get("error"),
    }
    entry["evidence_coverage"] = _evidence_coverage(
        row, rag_results, evidence_texts
    )
    return entry


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
    evidence_texts: Optional[dict[str, str]],
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
        "knowledge_cards_created": sum(
            row.get("knowledge_cards_created", 0) for row in sleeptime_rows
        ),
        "sleeptime_failures": sum(
            1 for row in sleeptime_rows if row.get("error")
        ),
        "evidence_coverage_threshold": EVIDENCE_COVERAGE_THRESHOLD,
        "evidence_retrieval_rate": (
            _mean([e["evidence_coverage"] for e in with_evidence])
            if evidence_texts is not None and with_evidence
            else None
        ),
    }
    return metrics


def _load_evidence_texts(dataset_path: Optional[Path]) -> Optional[dict[str, str]]:
    """Map every dialog id to its turn text for evidence-coverage checks."""
    if dataset_path is None:
        return None
    texts: dict[str, str] = {}
    for conversation in load_dataset(Path(dataset_path)):
        for session in conversation.sessions:
            for turn in session.turns:
                texts[turn.dialog_id] = turn.text
    return texts


def _evidence_coverage(
    row: dict,
    rag_results: list,
    evidence_texts: Optional[dict[str, str]],
) -> Optional[float]:
    """Heuristic: share of evidence turns whose stemmed content tokens appear
    in the retrieved card texts. Only computed when the dataset is supplied."""
    if evidence_texts is None:
        return None
    evidence_ids = row.get("evidence") or []
    if not evidence_ids:
        return None
    retrieved_tokens = set()
    for result in rag_results:
        if isinstance(result, dict):
            retrieved_tokens.update(stemmed_tokens(result.get("title", "")))
            retrieved_tokens.update(stemmed_tokens(result.get("episode", "")))
    covered = 0
    for evidence_id in evidence_ids:
        tokens = set(stemmed_tokens(evidence_texts.get(evidence_id, "")))
        if not tokens:
            continue
        overlap = len(tokens & retrieved_tokens) / len(tokens)
        if overlap >= EVIDENCE_COVERAGE_THRESHOLD:
            covered += 1
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


def _mean(values: list) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


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
