"""Render scores.json into a human-readable Markdown run summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from butly_core.io_utils import atomic_write_text


WORST_QUESTION_LIMIT = 10


class ReportError(ValueError):
    """Raised when the artifacts needed for a report are missing."""


def write_report(run_dir: Path) -> Path:
    """Write summary.md from a previously scored run and return its path."""
    run_path = Path(run_dir)
    scores = _read_json(run_path / "scores.json")
    if scores is None:
        raise ReportError(
            f"scores.json not found under {run_path}; run the score command first"
        )
    run_config = _read_json(run_path / "run_config.json") or {}
    error_count = _count_lines(run_path / "errors.jsonl")

    summary_path = run_path / "summary.md"
    atomic_write_text(summary_path, _render(scores, run_config, error_count))
    return summary_path


def _render(scores: dict, run_config: dict, error_count: int) -> str:
    official = scores.get("official", {})
    auxiliary = scores.get("auxiliary", {})
    butly = scores.get("butly", {})
    questions = scores.get("questions", [])

    lines = [
        f"# LoCoMo Evaluation Summary — {scores.get('run_id', 'unknown')}",
        "",
        f"- Generated: {scores.get('generated_at', 'unknown')}",
        f"- Questions scored: {scores.get('question_count', 0)}",
        f"- Dataset: {run_config.get('dataset_path', 'unknown')}",
        f"- Chat model: {run_config.get('model_name') or 'default'}"
        f" (connection: {run_config.get('connection') or 'default'})",
        f"- QA mode: {run_config.get('qa_mode', 'legacy/unknown')}",
        f"- Memory source: "
        f"{run_config.get('memory_reused_from_run_id') or 'built in this run'}",
        f"- Prompt locale: {run_config.get('locale') or 'legacy/unknown'}",
        f"- QA prompt version: "
        f"{run_config.get('qa_prompt_version') or 'legacy/unknown'}",
        f"- Scope: samples={_fmt_limit(run_config.get('sample_limit'))}, "
        f"sessions={_fmt_limit(run_config.get('session_limit'))}, "
        f"questions={_fmt_limit(run_config.get('question_limit'))}",
        f"- Errors recorded: {error_count} (see errors.jsonl)",
        "",
        "## Official-compatible scores",
        "",
        f"- Overall score: {_fmt(official.get('overall'))}",
        f"- No-information accuracy (category 5): "
        f"{_fmt(official.get('no_information_accuracy'))}",
        "",
        "| Category | Score | Questions |",
        "| --- | --- | --- |",
    ]
    # 公式 locomo10.json の category 番号との対応。採点ルールとデータ実体から:
    # 1 はカンマ区切り複数正解 (multi-hop の集約回答)、2 は When 系の時間推論、
    # 3 は世界知識推論 (セミコロン以降の補足を切る採点)、5 は no-info 検出。
    category_labels = {
        "1": "1 (multi-hop)",
        "2": "2 (temporal)",
        "3": "3 (open-domain)",
        "4": "4 (single-hop)",
        "5": "5 (adversarial)",
    }
    for category, entry in sorted(official.get("by_category", {}).items()):
        label = category_labels.get(category, category)
        lines.append(
            f"| {label} | {_fmt(entry.get('score'))} | {entry.get('count', 0)} |"
        )

    lines += [
        "",
        "## Auxiliary metrics (not official)",
        "",
        f"- Token F1 (whole answer): {_fmt(auxiliary.get('token_f1_mean'))}",
        f"- Exact match rate: {_fmt(auxiliary.get('exact_match_rate'))}",
        f"- Answer containment rate: "
        f"{_fmt(auxiliary.get('answer_containment_rate'))}",
        "",
        "## Butly metrics",
        "",
        f"- RAG trigger rate: {_fmt(butly.get('rag_trigger_rate'))}",
        f"- RAG trigger rate when correct / incorrect: "
        f"{_fmt(butly.get('rag_trigger_rate_when_correct'))} / "
        f"{_fmt(butly.get('rag_trigger_rate_when_incorrect'))}",
        f"- Evidence retrieval rate (provenance, chunk-level): "
        f"{_fmt(butly.get('evidence_retrieval_rate'))}",
        f"- Retrieved cards per question (mean): "
        f"{_fmt(butly.get('retrieved_cards_mean'), digits=2)}",
        f"- Latency ms mean / p50 / p95: "
        f"{_fmt(butly.get('latency_ms_mean'), digits=0)} / "
        f"{_fmt(butly.get('latency_ms_p50'), digits=0)} / "
        f"{_fmt(butly.get('latency_ms_p95'), digits=0)}",
        f"- Gatekeeper tier distribution: "
        f"{_fmt_distribution(butly.get('tier_distribution'))}",
        f"- need_intent distribution: "
        f"{_fmt_distribution(butly.get('need_intent_distribution'))}",
        f"- Classifier fallback rate: "
        f"{_fmt(butly.get('classifier_fallback_rate'))}"
        f" ({_fmt_distribution(butly.get('classifier_fallback_reasons'))})",
        f"- Intent floor rate (null→past_fact): "
        f"{_fmt(butly.get('intent_floor_rate'))}",
        f"- RAG source mode: "
        f"{_fmt_distribution(butly.get('rag_source_mode_distribution'))}",
        f"- RAG raw reference status: "
        f"{_fmt_distribution(butly.get('raw_reference_status_distribution'))}"
        f" (chars mean {_fmt(butly.get('raw_reference_chars_mean'), digits=0)}, "
        f"truncated rate {_fmt(butly.get('raw_reference_truncated_rate'))})",
        f"- Token usage (API-reported): prompt mean "
        f"{_fmt(butly.get('prompt_tokens_mean'), digits=0)} / total "
        f"{_fmt(butly.get('prompt_tokens_total'), digits=0)}, completion total "
        f"{_fmt(butly.get('completion_tokens_total'), digits=0)}",
        f"- Token usage per question, all QA-side calls: prompt mean "
        f"{_fmt(butly.get('qa_all_calls_prompt_tokens_mean'), digits=0)} / total "
        f"{_fmt(butly.get('qa_all_calls_prompt_tokens_total'), digits=0)}, "
        f"completion total "
        f"{_fmt(butly.get('qa_all_calls_completion_tokens_total'), digits=0)}",
        f"- Sleeptime token usage: prompt total "
        f"{_fmt(butly.get('sleeptime_prompt_tokens_total'), digits=0)}, "
        f"completion total "
        f"{_fmt(butly.get('sleeptime_completion_tokens_total'), digits=0)}",
        f"- Knowledge cards created by Sleeptime: "
        f"{butly.get('knowledge_cards_created', 0)}",
        f"- Sleeptime failures: {butly.get('sleeptime_failures', 0)}",
        f"- Sleeptime stage2 chunk failures: "
        f"{butly.get('stage2_chunk_failures', 0)} / "
        f"{butly.get('stage2_chunks', 0)} chunks",
    ]

    worst = [
        entry
        for entry in sorted(questions, key=lambda item: item.get("official_score", 0))
        if entry.get("official_score", 0) < 1.0
    ][:WORST_QUESTION_LIMIT]
    if worst:
        lines += ["", "## Lowest-scoring questions", ""]
        for entry in worst:
            lines += [
                f"### {entry.get('question_id')} "
                f"(category {entry.get('category')}, "
                f"score {_fmt(entry.get('official_score'))})",
                "",
                f"- Q: {entry.get('question')}",
                f"- Expected: {entry.get('expected_answer')}",
                f"- Predicted: {_truncate(entry.get('prediction'))}",
                f"- RAG triggered: {entry.get('rag_triggered')}"
                f" / retrieved cards: {entry.get('retrieved_card_count')}",
                "",
            ]

    lines += [
        "",
        "---",
        "",
        "Scores follow the official LoCoMo evaluation "
        "(https://github.com/snap-research/locomo, CC BY-NC 4.0); "
        "stemming uses an original-Porter implementation, so rare words may "
        "differ slightly from the official nltk stemmer.",
        "",
    ]
    return "\n".join(lines)


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if digits else f"{value:.0f}"


def _fmt_distribution(distribution: Optional[dict]) -> str:
    if not distribution:
        return "n/a"
    return ", ".join(f"{key}={count}" for key, count in distribution.items())


def _fmt_limit(value: Any) -> str:
    return "all" if value is None else str(value)


def _truncate(text: Any, limit: int = 300) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
