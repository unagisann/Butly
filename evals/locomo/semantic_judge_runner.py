"""Run the optional semantic judge over official-scored LoCoMo answers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from evals.semantic_judge import (
    JudgeConfig,
    SemanticJudge,
    SemanticJudgeError,
    judge_locomo_answer,
    summarize_locomo_judgments,
)

from .artifacts import safe_artifact_name, write_json
from .config import load_profile


logger = logging.getLogger(__name__)

SEMANTIC_SCORES_FILE_NAME = "semantic_scores.json"
_RESULT_SCHEMA_VERSION = 1

JudgeProgress = Callable[[int, int, str], None]


class LocomoJudgeError(ValueError):
    """Raised when a run cannot be judged safely."""


def resolve_judge_config(
    run_dir: Path,
    *,
    model_name: Optional[str] = None,
    connection: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
) -> Optional[JudgeConfig]:
    """Resolve explicit overrides over the run's persisted/profile judge."""
    run_path = Path(run_dir)
    run_config = _read_json_required(run_path / "run_config.json")
    if "judge" in run_config:
        raw = run_config.get("judge")
    else:
        raw = _profile_judge(run_config.get("profile_path"))
    mapping = dict(raw) if isinstance(raw, dict) else {}

    if model_name is not None:
        stripped_model = model_name.strip()
        if not stripped_model:
            raise LocomoJudgeError("judge model name must not be blank")
        previous_model = str(mapping.get("model_name") or "").strip()
        mapping["model_name"] = stripped_model
        if connection is None and stripped_model != previous_model:
            # A connection saved for the previous model must not silently be
            # reused after a model-only CLI override.  Leaving it absent lets
            # ProviderFactory infer the provider from the new model name.
            mapping.pop("connection", None)
    if connection is not None:
        stripped_connection = connection.strip()
        if not stripped_connection:
            raise LocomoJudgeError("judge connection must not be blank")
        mapping["connection"] = stripped_connection
    if max_output_tokens is not None:
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise LocomoJudgeError(
                "judge max output tokens must be a positive integer"
            )
        generation = mapping.get("generation_config") or {}
        if not isinstance(generation, dict):
            raise LocomoJudgeError(
                "judge generation_config must be a mapping"
            )
        mapping["generation_config"] = {
            **generation,
            "max_output_tokens": max_output_tokens,
        }

    if not mapping:
        return None
    try:
        config = JudgeConfig.from_mapping(mapping)
        if config is not None and not config.connection:
            from butly_core.llm.model_registry import normalize_model_ref

            normalize_model_ref(config.provider_config())
        return config
    except (TypeError, ValueError, RuntimeError) as exc:
        raise LocomoJudgeError(str(exc)) from exc


def run_locomo_semantic_judge(
    run_dir: Path,
    config: JudgeConfig,
    *,
    judge: Optional[SemanticJudge] = None,
    progress: Optional[JudgeProgress] = None,
) -> dict[str, Any]:
    """Judge every scored answer, resuming from matching atomic artifacts."""
    run_path = Path(run_dir)
    scores = _read_json_required(run_path / "scores.json")
    questions = scores.get("questions")
    if not isinstance(questions, list) or not questions:
        raise LocomoJudgeError(
            f"scores.json has no question rows: {run_path / 'scores.json'}"
        )

    active_judge = judge or SemanticJudge(config)
    if active_judge.config.signature() != config.signature():
        raise LocomoJudgeError(
            "injected judge config does not match the requested config"
        )

    run_id = str(scores.get("run_id") or run_path.name)
    results_root = run_path / "results" / "semantic_judge"
    judgments: list[dict[str, Any]] = []
    official_scores: dict[tuple[str, str], float] = {}
    seen_keys: set[tuple[str, str]] = set()
    total = len(questions)

    for index, row in enumerate(questions, start=1):
        if not isinstance(row, dict):
            raise LocomoJudgeError(
                f"scores.json question row {index} must be an object"
            )
        sample_id = _required_id(row.get("sample_id"), "sample_id")
        question_id = _required_id(row.get("question_id"), "question_id")
        key = (sample_id, question_id)
        if key in seen_keys:
            raise LocomoJudgeError(
                "scores.json contains duplicate question key: "
                f"{sample_id}/{question_id}"
            )
        seen_keys.add(key)

        category = _category(row.get("category"))
        expected_answer = _scoring_reference(
            row.get("expected_answer", ""), category
        )
        question = str(row.get("question") or "")
        prediction = str(row.get("prediction") or "")
        fingerprint = _input_fingerprint(
            sample_id=sample_id,
            question_id=question_id,
            category=category,
            question=question,
            expected_answer=expected_answer,
            prediction=prediction,
        )
        result_path = (
            results_root
            / safe_artifact_name(sample_id)
            / f"{safe_artifact_name(question_id)}.json"
        )
        existing = _read_json_optional(result_path)
        if _is_reusable(existing, config, fingerprint):
            result = existing
            state = "skipped"
        else:
            result = _judge_one(
                active_judge,
                config,
                run_id=run_id,
                sample_id=sample_id,
                question_id=question_id,
                category=category,
                question=question,
                expected_answer=expected_answer,
                prediction=prediction,
                input_fingerprint=fingerprint,
            )
            write_json(result_path, result)
            state = str(result.get("status") or "error")
        result = {
            **result,
            "artifact_path": result_path.relative_to(run_path).as_posix(),
        }
        judgments.append(result)

        official_score = row.get("official_score")
        if isinstance(official_score, (int, float)) and not isinstance(
            official_score, bool
        ):
            official_scores[key] = float(official_score)
        if progress is not None:
            progress(index, total, f"{sample_id}/{question_id} {state}")

    complete = [item for item in judgments if item.get("status") == "complete"]
    errors = [item for item in judgments if item.get("status") == "error"]
    summary = summarize_locomo_judgments(
        complete,
        official_scores=official_scores,
    )
    status = "completed" if len(complete) == total and not errors else "partial"
    payload = {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question_count": total,
        "judged_count": len(complete),
        "error_count": len(errors),
        "coverage": len(complete) / total,
        "judge": config.public_dict(),
        "config_signature": config.signature(),
        "question_set_fingerprint": calculate_question_set_fingerprint(
            scores,
            config,
        ),
        "summary": summary,
        "questions": judgments,
    }
    write_json(run_path / SEMANTIC_SCORES_FILE_NAME, payload)
    return payload


def _judge_one(
    judge: SemanticJudge,
    config: JudgeConfig,
    *,
    run_id: str,
    sample_id: str,
    question_id: str,
    category: int,
    question: str,
    expected_answer: Any,
    prediction: str,
    input_fingerprint: str,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        result = judge_locomo_answer(
            judge,
            sample_id=sample_id,
            question_id=question_id,
            category=category,
            question=question,
            expected_answer=expected_answer,
            prediction=prediction,
        )
    except Exception as exc:
        if isinstance(exc, SemanticJudgeError) and exc.fatal_configuration:
            # 同じparameter契約エラーを全設問で繰り返さない。Adapter側の
            # 1回補正でも解消しなかった場合だけrun自体を止める。
            raise
        logger.exception(
            "LoCoMo semantic judge failed for %s/%s",
            sample_id,
            question_id,
        )
        raw_response = getattr(exc, "raw_response", None)
        token_usage = getattr(exc, "token_usage", None)
        completion_metadata = getattr(exc, "completion_metadata", None)
        return {
            "schema_version": _RESULT_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "error",
            "generated_at": generated_at,
            "sample_id": sample_id,
            "question_id": question_id,
            "category": category,
            "config_signature": config.signature(),
            "input_fingerprint": input_fingerprint,
            "model": config.public_dict(),
            "error": f"{type(exc).__name__}: {exc}",
            "raw_response": (
                str(raw_response)[:4000] if raw_response is not None else None
            ),
            "token_usage": (
                token_usage if isinstance(token_usage, dict) else None
            ),
            "completion_metadata": (
                completion_metadata
                if isinstance(completion_metadata, dict)
                else None
            ),
        }
    return {
        **result,
        "run_id": run_id,
        "generated_at": generated_at,
        "config_signature": config.signature(),
        "input_fingerprint": input_fingerprint,
        "error": None,
    }


def _profile_judge(profile_path: Any) -> Optional[dict[str, Any]]:
    if not profile_path:
        return None
    path = Path(str(profile_path))
    if not path.is_file():
        return None
    try:
        return load_profile(path).judge
    except (OSError, ValueError) as exc:
        raise LocomoJudgeError(
            f"failed to load judge from profile {path}: {exc}"
        ) from exc


def _read_json_required(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocomoJudgeError(f"required artifact not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LocomoJudgeError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise LocomoJudgeError(f"JSON artifact must be an object: {path}")
    return payload


def _read_json_optional(path: Path) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable semantic judge artifact: %s", path)
        return None
    return payload if isinstance(payload, dict) else None


def _required_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LocomoJudgeError(f"scores.json question has no {field}")
    return text


def _category(value: Any) -> int:
    if isinstance(value, bool):
        raise LocomoJudgeError("question category must be between 1 and 5")
    try:
        category = int(value)
    except (TypeError, ValueError) as exc:
        raise LocomoJudgeError(
            "question category must be between 1 and 5"
        ) from exc
    if category not in {1, 2, 3, 4, 5}:
        raise LocomoJudgeError("question category must be between 1 and 5")
    return category


def _scoring_reference(value: Any, category: int) -> str:
    answer = str(value or "").strip()
    return answer.split(";", 1)[0].strip() if category == 3 else answer


def _input_fingerprint(
    **inputs: Any,
) -> str:
    canonical = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def calculate_question_set_fingerprint(
    scores: dict[str, Any],
    config: JudgeConfig,
) -> str:
    """Fingerprint all judge inputs plus the effective judge configuration.

    The aggregate artifact is only meaningful for the exact question,
    reference, prediction and prompt/model configuration that produced it.
    Official scores are intentionally excluded because they are not judge
    inputs and disagreements can be recomputed against their current values.
    """
    questions = scores.get("questions")
    if not isinstance(questions, list) or not questions:
        raise LocomoJudgeError("scores.json has no question rows")

    fingerprints: list[tuple[str, str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(questions, start=1):
        if not isinstance(row, dict):
            raise LocomoJudgeError(
                f"scores.json question row {index} must be an object"
            )
        sample_id = _required_id(row.get("sample_id"), "sample_id")
        question_id = _required_id(row.get("question_id"), "question_id")
        key = (sample_id, question_id)
        if key in seen_keys:
            raise LocomoJudgeError(
                "scores.json contains duplicate question key: "
                f"{sample_id}/{question_id}"
            )
        seen_keys.add(key)
        category = _category(row.get("category"))
        fingerprints.append(
            (
                sample_id,
                question_id,
                _input_fingerprint(
                    sample_id=sample_id,
                    question_id=question_id,
                    category=category,
                    question=str(row.get("question") or ""),
                    expected_answer=_scoring_reference(
                        row.get("expected_answer", ""),
                        category,
                    ),
                    prediction=str(row.get("prediction") or ""),
                ),
            )
        )

    canonical = json.dumps(
        {
            "config_signature": config.signature(),
            "input_fingerprints": sorted(fingerprints),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def semantic_scores_for_current_inputs(
    scores: dict[str, Any],
    semantic_scores: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return a semantic artifact only as valid for its current score inputs.

    Stale or malformed artifacts are reduced to explicit metadata with no old
    summary/question judgments, so reports and the web UI cannot accidentally
    present obsolete semantic metrics as current results.
    """
    if semantic_scores is None:
        return None
    if not isinstance(semantic_scores, dict):
        return _stale_semantic_scores(
            scores,
            {},
            reason="semantic_scores.json is not an object",
        )

    raw_judge = semantic_scores.get("judge")
    try:
        config = JudgeConfig.from_mapping(raw_judge)
        if config is None:
            raise LocomoJudgeError("semantic judge config is missing")
        expected_signature = config.signature()
        expected_fingerprint = calculate_question_set_fingerprint(
            scores,
            config,
        )
    except (LocomoJudgeError, TypeError, ValueError, RuntimeError) as exc:
        return _stale_semantic_scores(
            scores,
            semantic_scores,
            reason=f"cannot validate semantic judge inputs: {exc}",
        )

    if semantic_scores.get("config_signature") != expected_signature:
        return _stale_semantic_scores(
            scores,
            semantic_scores,
            reason="judge configuration or prompt version changed",
            current_fingerprint=expected_fingerprint,
        )
    if (
        semantic_scores.get("question_set_fingerprint")
        != expected_fingerprint
    ):
        return _stale_semantic_scores(
            scores,
            semantic_scores,
            reason="questions, references, or predictions changed",
            current_fingerprint=expected_fingerprint,
        )
    return _with_current_official_disagreements(scores, semantic_scores)


def build_semantic_question_details(
    scores: dict[str, Any],
    semantic_scores: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Merge official and current semantic results with review signals."""
    current = semantic_scores_for_current_inputs(scores, semantic_scores)
    semantic_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    default_judgment_status = (
        "stale"
        if isinstance(current, dict) and current.get("status") == "stale"
        else "unavailable"
    )
    if isinstance(current, dict) and current.get("status") != "stale":
        for item in current.get("questions") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("sample_id") or ""),
                str(item.get("question_id") or ""),
            )
            if all(key):
                semantic_by_key[key] = item

    rows: list[dict[str, Any]] = []
    for official in scores.get("questions") or []:
        if not isinstance(official, dict):
            continue
        key = (
            str(official.get("sample_id") or ""),
            str(official.get("question_id") or ""),
        )
        judgment = semantic_by_key.get(key) or {}
        judgment_status = (
            str(judgment.get("status") or default_judgment_status)
            if judgment
            else default_judgment_status
        )
        reasons: list[str] = []
        if judgment_status == "error":
            reasons.append("judge_error")
        if judgment.get("verdict") == "partial":
            reasons.append("partial")
        if judgment.get("contradiction") is True:
            reasons.append("contradiction")
        if judgment.get("missing_critical") is True:
            reasons.append("missing_critical")
        if judgment.get("confidence") == "low":
            reasons.append("low_confidence")

        official_score = official.get("official_score")
        semantic_score = judgment.get("normalized_score")
        disagreement = _official_disagreement(
            official_score,
            semantic_score,
        )
        if disagreement is not None:
            reasons.append(disagreement)

        rows.append(
            {
                "sample_id": official.get("sample_id"),
                "question_id": official.get("question_id"),
                "category": official.get("category"),
                "question": official.get("question"),
                "expected_answer": official.get("expected_answer"),
                "prediction": official.get("prediction"),
                "official_score": official_score,
                "semantic_status": judgment_status,
                "semantic_verdict": judgment.get("verdict"),
                "semantic_score": judgment.get("normalized_score"),
                "semantic_confidence": judgment.get("confidence"),
                "semantic_contradiction": judgment.get("contradiction"),
                "semantic_missing_critical": judgment.get(
                    "missing_critical"
                ),
                "semantic_reason": judgment.get("reason"),
                "semantic_error": judgment.get("error"),
                "official_disagreement": disagreement,
                "review_required": bool(reasons),
                "review_reasons": reasons,
            }
        )
    return current, rows


def _with_current_official_disagreements(
    scores: dict[str, Any],
    semantic_scores: dict[str, Any],
) -> dict[str, Any]:
    """Refresh derived disagreement counts without requiring re-judging."""
    official_by_key = {
        (
            str(item.get("sample_id") or ""),
            str(item.get("question_id") or ""),
        ): item.get("official_score")
        for item in (scores.get("questions") or [])
        if isinstance(item, dict)
    }
    counts = {
        "possible_false_negative_count": 0,
        "possible_false_positive_count": 0,
    }
    for item in semantic_scores.get("questions") or []:
        if not isinstance(item, dict) or item.get("status") != "complete":
            continue
        key = (
            str(item.get("sample_id") or ""),
            str(item.get("question_id") or ""),
        )
        disagreement = _official_disagreement(
            official_by_key.get(key),
            item.get("normalized_score"),
        )
        if disagreement is not None:
            counts[f"{disagreement}_count"] += 1

    stored_summary = semantic_scores.get("summary") or {}
    if (
        isinstance(stored_summary, dict)
        and stored_summary.get("official_disagreement") == counts
    ):
        return semantic_scores

    current = dict(semantic_scores)
    summary = dict(stored_summary) if isinstance(stored_summary, dict) else {}
    summary["official_disagreement"] = counts
    current["summary"] = summary
    return current


def _official_disagreement(
    official_score: Any,
    semantic_score: Any,
) -> Optional[str]:
    if (
        not isinstance(official_score, (int, float))
        or isinstance(official_score, bool)
        or not isinstance(semantic_score, (int, float))
        or isinstance(semantic_score, bool)
    ):
        return None
    if float(official_score) < 0.5 and float(semantic_score) >= 0.75:
        return "possible_false_negative"
    if float(official_score) >= 0.5 and float(semantic_score) < 0.5:
        return "possible_false_positive"
    return None


def _stale_semantic_scores(
    scores: dict[str, Any],
    semantic_scores: dict[str, Any],
    *,
    reason: str,
    current_fingerprint: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "schema_version": semantic_scores.get("schema_version"),
        "run_id": scores.get("run_id") or semantic_scores.get("run_id"),
        "status": "stale",
        "original_status": semantic_scores.get("status"),
        "generated_at": semantic_scores.get("generated_at"),
        "question_count": scores.get("question_count"),
        "judged_count": 0,
        "error_count": None,
        "coverage": None,
        "judge": semantic_scores.get("judge"),
        "config_signature": semantic_scores.get("config_signature"),
        "question_set_fingerprint": semantic_scores.get(
            "question_set_fingerprint"
        ),
        "current_question_set_fingerprint": current_fingerprint,
        "stale_reason": reason,
        "rejudge_required": True,
        "summary": {},
        "questions": [],
    }


def _is_reusable(
    payload: Optional[dict[str, Any]],
    config: JudgeConfig,
    input_fingerprint: str,
) -> bool:
    return bool(
        payload
        and payload.get("status") == "complete"
        and payload.get("config_signature") == config.signature()
        and payload.get("input_fingerprint") == input_fingerprint
    )
