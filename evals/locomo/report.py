"""Render scores.json into a human-readable Markdown run summary."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from butly_core.io_utils import atomic_write_text


WORST_QUESTION_LIMIT = 10


logger = logging.getLogger(__name__)


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
    chat_model = _resolve_chat_model(run_path, run_config)
    semantic_scores = _read_json(run_path / "semantic_scores.json")
    if semantic_scores is not None:
        from .semantic_judge_runner import semantic_scores_for_current_inputs

        semantic_scores = semantic_scores_for_current_inputs(
            scores,
            semantic_scores,
        )
    error_count = _count_lines(run_path / "errors.jsonl")

    summary_path = run_path / "summary.md"
    atomic_write_text(
        summary_path,
        _render(
            scores,
            run_config,
            error_count,
            semantic_scores=semantic_scores,
            chat_model=chat_model,
        ),
    )
    return summary_path


def _render(
    scores: dict,
    run_config: dict,
    error_count: int,
    *,
    semantic_scores: Optional[dict] = None,
    chat_model: Optional[dict[str, Optional[str]]] = None,
) -> str:
    official = scores.get("official", {})
    scoring = scores.get("scoring", {})
    auxiliary = scores.get("auxiliary", {})
    butly = scores.get("butly", {})
    questions = scores.get("questions", [])
    chat_model = chat_model or {}
    chat_model_name = (
        chat_model.get("model_name")
        or run_config.get("model_name")
        or "unknown"
    )
    chat_connection = (
        chat_model.get("connection")
        or run_config.get("connection")
        or "unknown"
    )

    lines = [
        f"# LoCoMo Evaluation Summary — {scores.get('run_id', 'unknown')}",
        "",
        f"- Generated: {scores.get('generated_at', 'unknown')}",
        f"- Questions scored: {scores.get('question_count', 0)}",
        f"- Dataset: {run_config.get('dataset_path', 'unknown')}",
        f"- Chat model: {chat_model_name}"
        f" (connection: {chat_connection})",
        f"- QA mode: {run_config.get('qa_mode', 'legacy/unknown')}",
        f"- QA memory mode: "
        f"{run_config.get('qa_memory_mode', 'normal/legacy')}",
        f"- Memory source: "
        f"{run_config.get('memory_reused_from_run_id') or 'built in this run'}",
        f"- Prompt locale: {run_config.get('locale') or 'legacy/unknown'}",
        f"- Dataset locale: "
        f"{run_config.get('dataset_locale') or scoring.get('locale') or 'unknown'}",
        f"- QA prompt version: "
        f"{run_config.get('qa_prompt_version') or 'legacy/unknown'}",
        f"- Scoring: {scoring.get('method') or 'legacy/unknown'} "
        f"(official-compatible: {scoring.get('official_compatible')})",
        f"- Scope: samples={_fmt_limit(run_config.get('sample_limit'))}, "
        f"sessions={_fmt_limit(run_config.get('session_limit'))}, "
        f"questions={_fmt_limit(run_config.get('question_limit'))}",
        f"- Errors recorded: {error_count} (see errors.jsonl)",
        f"- QA transport retries: {butly.get('qa_retry_total', 0)} across "
        f"{_fmt(butly.get('qa_retry_question_rate'))} of questions "
        f"({_fmt_distribution(butly.get('qa_retry_reason_distribution'))})",
        "",
        (
            "## Japanese-localized scores (not comparable to official English)"
            if scoring.get("locale") == "ja"
            else "## Official-compatible scores"
        ),
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
    ]
    if semantic_scores is not None:
        lines += _render_semantic_judge(semantic_scores)
    lines += [
        "",
        "## Butly metrics",
        "",
        f"- RAG trigger rate: {_fmt(butly.get('rag_trigger_rate'))}",
        f"- RAG trigger rate when correct / incorrect: "
        f"{_fmt(butly.get('rag_trigger_rate_when_correct'))} / "
        f"{_fmt(butly.get('rag_trigger_rate_when_incorrect'))}",
        f"- Retrieval recall @1 / @3 / @20: "
        f"{_fmt(butly.get('retrieval_recall_at_1'))} / "
        f"{_fmt(butly.get('retrieval_recall_at_3'))} / "
        f"{_fmt(butly.get('retrieval_recall_at_20'))}",
        f"- Retrieval query generation rate: "
        f"{_fmt(butly.get('retrieval_query_rate'))}",
        f"- Dual-query original / rewritten recall at top 3: "
        f"{_fmt(butly.get('dual_query_original_recall_at_3'))} / "
        f"{_fmt(butly.get('dual_query_rewrite_recall_at_3'))}",
        f"- Dual-query rescue / harm rate at top 3: "
        f"{_fmt(butly.get('dual_query_rescue_rate_at_3'))} / "
        f"{_fmt(butly.get('dual_query_harm_rate_at_3'))}",
        f"- Reranker completion / fallback rate: "
        f"{_fmt(butly.get('reranker_completion_rate'))} / "
        f"{_fmt(butly.get('reranker_fallback_rate'))}",
        f"- Reranker rescue / harm rate at top 3: "
        f"{_fmt(butly.get('reranker_rescue_rate_at_3'))} / "
        f"{_fmt(butly.get('reranker_harm_rate_at_3'))}",
        f"- Reranker latency ms p50 / p95: "
        f"{_fmt(butly.get('reranker_latency_ms_p50'), digits=0)} / "
        f"{_fmt(butly.get('reranker_latency_ms_p95'), digits=0)}",
        f"- Evidence fusion completion / fallback rate: "
        f"{_fmt(butly.get('evidence_fusion_completion_rate'))} / "
        f"{_fmt(butly.get('evidence_fusion_fallback_rate'))}",
        f"- Evidence fusion rescue / harm rate at top 3 vs hybrid: "
        f"{_fmt(butly.get('evidence_fusion_rescue_rate_at_3'))} / "
        f"{_fmt(butly.get('evidence_fusion_harm_rate_at_3'))}",
        f"- Evidence fusion latency ms p50 / p95 (cache hit rate): "
        f"{_fmt(butly.get('evidence_fusion_latency_ms_p50'), digits=0)} / "
        f"{_fmt(butly.get('evidence_fusion_latency_ms_p95'), digits=0)} "
        f"({_fmt(butly.get('evidence_fusion_cache_hit_rate'))})",
        f"- Evidence retrieval rate (provenance, source_files): "
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
        f" (files mean {_fmt(butly.get('raw_reference_files_mean'), digits=1)}, "
        f"chars mean {_fmt(butly.get('raw_reference_chars_mean'), digits=0)}, "
        f"truncated rate {_fmt(butly.get('raw_reference_truncated_rate'))}, "
        f"neighbor radius "
        f"{_fmt_distribution(butly.get('raw_reference_neighbor_radius_distribution'))}, "
        f"inferred files mean "
        f"{_fmt(butly.get('raw_reference_inferred_neighbor_files_mean'), digits=1)})",
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
        f"- Card source granularity (per-card / chunk-wide): "
        f"{butly.get('knowledge_source_files_card', 0)} / "
        f"{butly.get('knowledge_source_files_chunk', 0)}",
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


def _render_semantic_judge(semantic_scores: dict) -> list[str]:
    summary = semantic_scores.get("summary") or {}
    judge = semantic_scores.get("judge") or summary.get("model") or {}
    if semantic_scores.get("status") == "stale":
        return [
            "",
            "## Semantic judge (auxiliary, not official)",
            "",
            "- Status: stale (re-judging required)",
            f"- Model: {judge.get('model_name') or 'unknown'} "
            f"(connection: {judge.get('connection') or 'inferred/default'})",
            "- Reason: "
            f"{semantic_scores.get('stale_reason') or 'judge inputs changed'}",
            "- Previous semantic metrics and per-question judgments are hidden.",
        ]
    disagreements = summary.get("official_disagreement") or {}
    question_count = int(semantic_scores.get("question_count") or 0)
    judged_count = int(semantic_scores.get("judged_count") or 0)
    lines = [
        "",
        "## Semantic judge (auxiliary, not official)",
        "",
        f"- Status: {semantic_scores.get('status') or 'unknown'}",
        f"- Model: {judge.get('model_name') or 'unknown'} "
        f"(connection: {judge.get('connection') or 'inferred/default'})",
        f"- Prompt version: {summary.get('prompt_version') or 'unknown'}",
        f"- Coverage: {judged_count}/{question_count} "
        f"({_fmt(semantic_scores.get('coverage'))}); errors: "
        f"{semantic_scores.get('error_count', 0)}",
        f"- Normalized semantic score mean: "
        f"{_fmt(summary.get('normalized_score_mean'))}",
        f"- Pass / partial / fail rate: "
        f"{_fmt(summary.get('pass_rate'))} / "
        f"{_fmt(summary.get('partial_rate'))} / "
        f"{_fmt(summary.get('fail_rate'))}",
        f"- Contradiction / critical-missing rate: "
        f"{_fmt(summary.get('contradiction_rate'))} / "
        f"{_fmt(summary.get('missing_critical_rate'))}",
        f"- Possible official false negatives / false positives: "
        f"{disagreements.get('possible_false_negative_count', 0)} / "
        f"{disagreements.get('possible_false_positive_count', 0)}",
        "",
        "| Category | Semantic score | Judged | Pass | Partial | Fail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    categories = summary.get("categories") or {}
    for category in ("1", "2", "3", "4", "5"):
        entry = categories.get(category) or {}
        lines.append(
            f"| {category} | {_fmt(entry.get('normalized_score_mean'))} | "
            f"{entry.get('judged_count', 0)} | "
            f"{_fmt(entry.get('pass_rate'))} | "
            f"{_fmt(entry.get('partial_rate'))} | "
            f"{_fmt(entry.get('fail_rate'))} |"
        )
    return lines


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_chat_model(
    run_path: Path,
    run_config: dict,
) -> dict[str, Optional[str]]:
    """Resolve the model actually recorded by QA, then persisted config/profile."""
    recorded = _read_recorded_chat_model(
        run_path / "results" / "qa_results.jsonl"
    )
    model_name = recorded.get("model_name") or _optional_text(
        run_config.get("model_name")
    )
    connection = recorded.get("connection") or _optional_text(
        run_config.get("connection")
    )

    profile_path = _optional_text(run_config.get("profile_path"))
    if profile_path and (model_name is None or connection is None):
        from .config import ProfileError, load_profile

        try:
            profile = load_profile(Path(profile_path))
        except (OSError, ProfileError) as exc:
            logger.warning(
                "Could not resolve report chat model from profile %s: %s",
                profile_path,
                exc,
            )
        else:
            chat = profile.sections.get("chat") or {}
            model_name = model_name or _optional_text(chat.get("model_name"))
            connection = connection or _optional_text(chat.get("connection"))

    return {"model_name": model_name, "connection": connection}


def _read_recorded_chat_model(path: Path) -> dict[str, Optional[str]]:
    if not path.is_file():
        return {"model_name": None, "connection": None}
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Ignoring invalid QA result at %s:%d: %s",
                        path,
                        line_number,
                        exc,
                    )
                    continue
                diagnostics = row.get("diagnostics") or {}
                model_name = _optional_text(diagnostics.get("model"))
                connection = _optional_text(diagnostics.get("connection_id"))
                if model_name is not None or connection is not None:
                    return {
                        "model_name": model_name,
                        "connection": connection,
                    }
    except OSError as exc:
        logger.warning("Could not read QA model metadata from %s: %s", path, exc)
    return {"model_name": None, "connection": None}


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


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
