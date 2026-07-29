"""Japanese production-dialogue A/B runner for memory injection policies."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import statistics
import time
import unicodedata
from typing import Any, Optional

from butly_core.chat.types import ChatRequest
from evals.locomo.artifacts import (
    copy_latest_trace,
    count_knowledge_cards,
    resolve_retrieved_card_ids,
    safe_artifact_name,
    write_json,
)
from evals.locomo.config import EvaluationProfile, load_profile
from evals.locomo.sleeptime_runner import SleeptimeRunner
from evals.locomo.workspace import EvaluationWorkspace, IndependentQAWorkspace


logger = logging.getLogger(__name__)

POLICIES = ("intent_gated", "candidates")
_CATEGORIES = frozenset(
    {"memory_required", "memory_irrelevant", "memory_optional"}
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,179}$")
_INSTANCE_NAME = "ja_dialogue_ab"
_CHECKPOINT_NAME = "dialogue_ab.json"


class DialogueABError(ValueError):
    """Raised when a dialogue A/B dataset or run is invalid."""


@dataclass(frozen=True)
class MemorySeed:
    seed_id: str
    timestamp: datetime
    user: str
    assistant: str


@dataclass(frozen=True)
class DialoguePrompt:
    prompt_id: str
    category: str
    text: str
    expected_memory_behavior: str
    target_memory_ids: tuple[str, ...]
    expected_terms: tuple[str, ...]
    review_point: Optional[str]


@dataclass(frozen=True)
class DialogueDataset:
    dataset_id: str
    locale: str
    memory_seed: tuple[MemorySeed, ...]
    prompts: tuple[DialoguePrompt, ...]


@dataclass(frozen=True)
class DialogueABConfig:
    dataset_path: Path
    output_dir: Path
    run_id: str
    profile_path: Path


def load_dialogue_dataset(path: Path) -> DialogueDataset:
    """Load and validate the dedicated Japanese dialogue fixture."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DialogueABError(f"dialogue dataset not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise DialogueABError(
            f"invalid dialogue dataset JSON: line {exc.lineno}, "
            f"column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise DialogueABError("dialogue dataset root must be an object")
    if payload.get("schema_version") != 1:
        raise DialogueABError("unsupported dialogue dataset schema_version")

    dataset_id = _required_text(payload.get("dataset_id"), "dataset_id")
    locale = _required_text(payload.get("locale"), "locale")
    if locale != "ja":
        raise DialogueABError("dialogue dataset locale must be 'ja'")

    raw_seed = payload.get("memory_seed")
    if not isinstance(raw_seed, list) or not raw_seed:
        raise DialogueABError("memory_seed must be a non-empty array")
    seeds = []
    seed_ids = set()
    for index, item in enumerate(raw_seed, start=1):
        context = f"memory_seed[{index}]"
        if not isinstance(item, dict):
            raise DialogueABError(f"{context} must be an object")
        seed_id = _safe_id(item.get("id"), f"{context}.id")
        if seed_id in seed_ids:
            raise DialogueABError(f"duplicate memory seed id: {seed_id}")
        seed_ids.add(seed_id)
        date_text = _required_text(item.get("date"), f"{context}.date")
        try:
            source_date = datetime.fromisoformat(date_text).date()
        except ValueError as exc:
            raise DialogueABError(
                f"{context}.date must use YYYY-MM-DD"
            ) from exc
        seeds.append(
            MemorySeed(
                seed_id=seed_id,
                timestamp=datetime.combine(
                    source_date,
                    datetime_time(hour=12),
                    tzinfo=timezone.utc,
                ),
                user=_required_text(item.get("user"), f"{context}.user"),
                assistant=_required_text(
                    item.get("assistant"), f"{context}.assistant"
                ),
            )
        )

    raw_prompts = payload.get("prompts")
    if not isinstance(raw_prompts, list) or not raw_prompts:
        raise DialogueABError("prompts must be a non-empty array")
    prompts = []
    prompt_ids = set()
    for index, item in enumerate(raw_prompts, start=1):
        context = f"prompts[{index}]"
        if not isinstance(item, dict):
            raise DialogueABError(f"{context} must be an object")
        prompt_id = _safe_id(item.get("id"), f"{context}.id")
        if prompt_id in prompt_ids:
            raise DialogueABError(f"duplicate prompt id: {prompt_id}")
        prompt_ids.add(prompt_id)
        category = _required_text(item.get("category"), f"{context}.category")
        if category not in _CATEGORIES:
            raise DialogueABError(
                f"{context}.category must be one of {sorted(_CATEGORIES)}"
            )
        target_ids = _text_array(
            item.get("target_memory_ids", []),
            f"{context}.target_memory_ids",
        )
        unknown_targets = set(target_ids) - seed_ids
        if unknown_targets:
            raise DialogueABError(
                f"{context} references unknown memory seeds: "
                f"{sorted(unknown_targets)}"
            )
        prompts.append(
            DialoguePrompt(
                prompt_id=prompt_id,
                category=category,
                text=_required_text(item.get("prompt"), f"{context}.prompt"),
                expected_memory_behavior=_required_text(
                    item.get("expected_memory_behavior"),
                    f"{context}.expected_memory_behavior",
                ),
                target_memory_ids=tuple(target_ids),
                expected_terms=tuple(
                    _text_array(
                        item.get("expected_terms", []),
                        f"{context}.expected_terms",
                    )
                ),
                review_point=_optional_text(item.get("review_point")),
            )
        )
    return DialogueDataset(
        dataset_id=dataset_id,
        locale=locale,
        memory_seed=tuple(seeds),
        prompts=tuple(prompts),
    )


async def run_dialogue_ab(config: DialogueABConfig) -> dict[str, Any]:
    dataset = load_dialogue_dataset(config.dataset_path)
    profile = load_profile(config.profile_path)
    workspace = EvaluationWorkspace.create(
        config.output_dir,
        run_id=config.run_id,
        clean=False,
    )
    workspace.write_run_config(
        {
            "schema_version": 1,
            "run_type": "dialogue_ab",
            "run_id": config.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset_path": str(config.dataset_path.resolve()),
            "dataset_id": dataset.dataset_id,
            "locale": dataset.locale,
            "output_dir": str(config.output_dir.resolve()),
            "profile_path": str(config.profile_path.resolve()),
            "policies": list(POLICIES),
            "prompt_count": len(dataset.prompts),
            "qa_isolation": "independent",
        }
    )
    _write_checkpoint(
        workspace,
        {
            "schema_version": 1,
            "run_id": config.run_id,
            "status": "running",
            "seed_completed": False,
        },
    )
    return await _execute(workspace, config, dataset, profile)


async def resume_dialogue_ab(run_dir: Path) -> dict[str, Any]:
    workspace = EvaluationWorkspace.open(run_dir)
    payload = _read_json(workspace.run_config_path)
    if payload.get("run_type") != "dialogue_ab":
        raise DialogueABError(f"not a dialogue A/B run: {run_dir}")
    config = DialogueABConfig(
        dataset_path=Path(payload["dataset_path"]),
        output_dir=Path(payload["output_dir"]),
        run_id=str(payload["run_id"]),
        profile_path=Path(payload["profile_path"]),
    )
    dataset = load_dialogue_dataset(config.dataset_path)
    profile = load_profile(config.profile_path)
    return await _execute(workspace, config, dataset, profile)


async def _execute(
    workspace: EvaluationWorkspace,
    config: DialogueABConfig,
    dataset: DialogueDataset,
    profile: EvaluationProfile,
) -> dict[str, Any]:
    checkpoint = _load_checkpoint(workspace)
    instance_dir = workspace.instances_dir / _INSTANCE_NAME
    if not checkpoint.get("seed_completed") or not instance_dir.is_dir():
        _prepare_seed_memory(workspace, dataset, profile)
        checkpoint = {
            **checkpoint,
            "status": "running",
            "seed_completed": True,
        }
        _write_checkpoint(workspace, checkpoint)

    completed = _completed_result_keys(workspace)
    total = len(dataset.prompts) * len(POLICIES)
    for policy in POLICIES:
        with IndependentQAWorkspace(instance_dir) as isolated:
            for prompt in dataset.prompts:
                result_key = (policy, prompt.prompt_id)
                if result_key in completed:
                    continue
                isolated.reset()
                runtime = isolated.create_runtime()
                _configure_policy(runtime, isolated.instance_name, policy)
                result = await _run_prompt(
                    runtime=runtime,
                    workspace=workspace,
                    isolated=isolated,
                    dataset=dataset,
                    prompt=prompt,
                    policy=policy,
                )
                result_path = _result_path(
                    workspace,
                    policy,
                    prompt.prompt_id,
                )
                write_json(result_path, result)
                completed.add(result_key)
                progress = 10.0 + 85.0 * len(completed) / total
                _emit_progress(
                    progress,
                    policy,
                    f"{prompt.prompt_id} ({len(completed)}/{total}) completed",
                )
                _write_checkpoint(
                    workspace,
                    {
                        **checkpoint,
                        "status": "running",
                        "seed_completed": True,
                        "completed_prompts": len(completed),
                        "total_prompts": total,
                        "last_policy": policy,
                        "last_prompt_id": prompt.prompt_id,
                    },
                )

    scores = build_dialogue_scores(
        dataset,
        _load_results(workspace),
        run_id=workspace.run_id,
        knowledge_cards=count_knowledge_cards(
            instance_dir / "butly_memory.db"
        ),
    )
    write_json(workspace.run_dir / "scores.json", scores)
    _write_checkpoint(
        workspace,
        {
            **checkpoint,
            "status": "completed",
            "seed_completed": True,
            "completed_prompts": total,
            "total_prompts": total,
        },
    )
    _emit_progress(100.0, "complete", "Japanese dialogue A/B completed")
    return scores


def _prepare_seed_memory(
    workspace: EvaluationWorkspace,
    dataset: DialogueDataset,
    profile: EvaluationProfile,
) -> None:
    instance_dir = workspace.instances_dir / _INSTANCE_NAME
    if instance_dir.exists():
        shutil.rmtree(instance_dir)
    runtime = workspace.create_runtime()
    success, message = runtime.instance_manager.create_instance(
        _INSTANCE_NAME,
        (
            "あなたはユーザーと自然に会話する日本語の対話アシスタントです。"
            "過去の記憶は、現在の依頼に役立つ場合だけ自然に利用してください。"
            "関係のない記憶や個人情報を唐突に列挙せず、分からないことを"
            "記憶から推測して断定しないでください。"
        ),
        agent_profile={"ai_name": "Butly", "locale": "ja"},
        user_profile={"user_name": "ユーザー", "preferred_call": "あなた"},
    )
    if not success:
        raise RuntimeError(f"failed to create dialogue A/B instance: {message}")
    _configure_base_instance(runtime, _INSTANCE_NAME, profile)
    memory = runtime.get_instance_components(_INSTANCE_NAME)["memory"]
    for seed in dataset.memory_seed:
        saved = memory.save_single_turn(
            seed.user,
            seed.assistant,
            meta={
                "source": "eval",
                "lane": "direct",
                "dialogue_ab_seed_id": seed.seed_id,
                "original_timestamp": seed.timestamp.isoformat(),
            },
            created_at=seed.timestamp,
        )
        if saved is None:
            raise RuntimeError(f"failed to save memory seed: {seed.seed_id}")

    _emit_progress(2.0, "seed", "Memory seed saved; running Sleeptime")
    runner = SleeptimeRunner(
        workspace.create_sleeptime(),
        run_id=workspace.run_id,
        log_path=workspace.results_dir / "sleeptime_log.jsonl",
    )
    runner.run(
        sample_id=dataset.dataset_id,
        session_id="memory_seed",
        instance_name=_INSTANCE_NAME,
        session_now=max(seed.timestamp for seed in dataset.memory_seed),
    )
    _emit_progress(10.0, "seed", "Memory seed knowledgeization completed")


def _configure_base_instance(
    runtime: Any,
    instance_name: str,
    profile: EvaluationProfile,
) -> None:
    config = runtime.instance_manager.get_instance_config(instance_name)
    config.setdefault("sleeptime", {}).setdefault("update_targets", {}).update(
        {
            "digest": True,
            "recent_snapshot": True,
            "key_memory": False,
            "knowledge_cards": True,
            "raw_memory_cache": True,
        }
    )
    for section, overrides in profile.sections.items():
        _merge_mapping(config.setdefault(section, {}), overrides)
    config.setdefault("prompts", {})["allow_user_overrides"] = False
    updated, message = runtime.instance_manager.update_instance_config(
        instance_name,
        config,
    )
    if not updated:
        raise RuntimeError(f"failed to configure dialogue A/B instance: {message}")


def _configure_policy(runtime: Any, instance_name: str, policy: str) -> None:
    if policy not in POLICIES:
        raise DialogueABError(f"unsupported dialogue A/B policy: {policy}")
    config = runtime.instance_manager.get_instance_config(instance_name)
    probe = config.setdefault("memory_probe", {})
    probe["retrieval_execution"] = "always"
    probe["injection_policy"] = policy
    updated, message = runtime.instance_manager.update_instance_config(
        instance_name,
        config,
    )
    if not updated:
        raise RuntimeError(f"failed to set injection policy: {message}")


async def _run_prompt(
    *,
    runtime: Any,
    workspace: EvaluationWorkspace,
    isolated: IndependentQAWorkspace,
    dataset: DialogueDataset,
    prompt: DialoguePrompt,
    policy: str,
) -> dict[str, Any]:
    request = ChatRequest(
        text=prompt.text,
        instance_name=isolated.instance_name,
        use_rag=True,
        use_google_search=False,
        use_web_search=False,
        source="api",
        metadata={
            "evaluation": "dialogue_ab",
            "policy": policy,
            "prompt_id": prompt.prompt_id,
        },
    )
    started = time.perf_counter()
    response = await runtime.chat(request)
    latency_ms = int((time.perf_counter() - started) * 1000)
    copied_trace = copy_latest_trace(
        isolated.instance_dir,
        workspace.traces_dir,
        prompt.prompt_id,
        sample_id=policy,
    )
    if copied_trace is None:
        raise RuntimeError(
            f"ButlyRuntime completed without a Trace: {policy}/{prompt.prompt_id}"
        )

    debug_info = response.debug_info or {}
    rag = debug_info.get("rag") if isinstance(debug_info.get("rag"), dict) else {}
    rag_results = rag.get("results") if isinstance(rag.get("results"), list) else []
    retrieval = (
        rag.get("retrieval")
        if isinstance(rag.get("retrieval"), dict)
        else {}
    )
    token_usage = (
        debug_info.get("token_usage")
        if isinstance(debug_info.get("token_usage"), dict)
        else {}
    )
    total_usage = (
        debug_info.get("token_usage_total")
        if isinstance(debug_info.get("token_usage_total"), dict)
        else {}
    )
    target_matches = {
        term: _contains_text(response.text, term)
        for term in prompt.expected_terms
    }
    distinctive_terms = _distinctive_seed_terms(dataset)
    seed_mentions = [
        term for term in distinctive_terms if _contains_text(response.text, term)
    ]
    return {
        "schema_version": 1,
        "run_id": workspace.run_id,
        "policy": policy,
        "prompt_id": prompt.prompt_id,
        "category": prompt.category,
        "prompt": prompt.text,
        "expected_memory_behavior": prompt.expected_memory_behavior,
        "target_memory_ids": list(prompt.target_memory_ids),
        "expected_terms": list(prompt.expected_terms),
        "review_point": prompt.review_point,
        "response": response.text,
        "tier": response.tier,
        "need": response.need,
        "latency_ms": latency_ms,
        "rag_triggered": bool(rag_results),
        "search_executed": bool(retrieval.get("executed")),
        "injection_reason": retrieval.get("injection_reason"),
        "retrieved_card_ids": resolve_retrieved_card_ids(
            isolated.instance_dir / "butly_memory.db",
            [item for item in rag_results if isinstance(item, dict)],
        ),
        "prompt_tokens": _optional_int(token_usage.get("prompt_tokens")),
        "completion_tokens": _optional_int(
            token_usage.get("completion_tokens")
        ),
        "total_prompt_tokens": _optional_int(
            total_usage.get("prompt_tokens")
        ),
        "target_term_matches": target_matches,
        "target_term_recall": (
            sum(target_matches.values()) / len(target_matches)
            if target_matches
            else None
        ),
        "seed_term_mentions": seed_mentions,
        "trace_path": copied_trace.relative_to(
            workspace.run_dir
        ).as_posix(),
    }


def build_dialogue_scores(
    dataset: DialogueDataset,
    results: list[dict[str, Any]],
    *,
    run_id: str,
    knowledge_cards: int,
) -> dict[str, Any]:
    """Aggregate automatic proxies while retaining responses for human review."""
    by_policy: dict[str, list[dict[str, Any]]] = {
        policy: [] for policy in POLICIES
    }
    by_key = {}
    for result in results:
        policy = result.get("policy")
        prompt_id = result.get("prompt_id")
        if policy not in by_policy or not isinstance(prompt_id, str):
            continue
        by_policy[policy].append(result)
        by_key[(policy, prompt_id)] = result

    policy_scores = {
        policy: _summarize_policy(items)
        for policy, items in by_policy.items()
    }
    prompts = []
    for prompt in dataset.prompts:
        arms = {
            policy: by_key.get((policy, prompt.prompt_id))
            for policy in POLICIES
        }
        baseline = arms["intent_gated"] or {}
        candidate = arms["candidates"] or {}
        prompts.append(
            {
                "prompt_id": prompt.prompt_id,
                "category": prompt.category,
                "prompt": prompt.text,
                "expected_memory_behavior": prompt.expected_memory_behavior,
                "review_point": prompt.review_point,
                "prompt_tokens_delta": _numeric_delta(
                    candidate.get("prompt_tokens"),
                    baseline.get("prompt_tokens"),
                ),
                "latency_ms_delta": _numeric_delta(
                    candidate.get("latency_ms"),
                    baseline.get("latency_ms"),
                ),
                "response_changed": (
                    candidate.get("response") != baseline.get("response")
                    if baseline and candidate
                    else None
                ),
                "arms": arms,
            }
        )
    baseline_summary = policy_scores["intent_gated"]
    candidate_summary = policy_scores["candidates"]
    return {
        "schema_version": 1,
        "run_type": "dialogue_ab",
        "run_id": run_id,
        "dataset_id": dataset.dataset_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prompt_count": len(dataset.prompts),
        "knowledge_cards_created": knowledge_cards,
        "policies": policy_scores,
        "comparison": {
            "rag_trigger_rate_delta": _numeric_delta(
                candidate_summary.get("rag_trigger_rate"),
                baseline_summary.get("rag_trigger_rate"),
            ),
            "prompt_tokens_mean_delta": _numeric_delta(
                candidate_summary.get("prompt_tokens_mean"),
                baseline_summary.get("prompt_tokens_mean"),
            ),
            "latency_ms_mean_delta": _numeric_delta(
                candidate_summary.get("latency_ms_mean"),
                baseline_summary.get("latency_ms_mean"),
            ),
            "required_target_recall_delta": _numeric_delta(
                candidate_summary.get("required_target_recall"),
                baseline_summary.get("required_target_recall"),
            ),
            "irrelevant_seed_mention_rate_delta": _numeric_delta(
                candidate_summary.get("irrelevant_seed_mention_rate"),
                baseline_summary.get("irrelevant_seed_mention_rate"),
            ),
        },
        "prompts": prompts,
    }


def _summarize_policy(items: list[dict[str, Any]]) -> dict[str, Any]:
    required = [
        item
        for item in items
        if item.get("category") == "memory_required"
        and isinstance(item.get("target_term_recall"), (int, float))
    ]
    irrelevant = [
        item for item in items if item.get("category") == "memory_irrelevant"
    ]
    return {
        "prompt_count": len(items),
        "rag_trigger_rate": _rate(
            bool(item.get("rag_triggered")) for item in items
        ),
        "search_execution_rate": _rate(
            bool(item.get("search_executed")) for item in items
        ),
        "prompt_tokens_total": _sum_numeric(
            item.get("prompt_tokens") for item in items
        ),
        "prompt_tokens_mean": _mean_numeric(
            item.get("prompt_tokens") for item in items
        ),
        "total_prompt_tokens_total": _sum_numeric(
            item.get("total_prompt_tokens") for item in items
        ),
        "latency_ms_mean": _mean_numeric(
            item.get("latency_ms") for item in items
        ),
        "latency_ms_p95": _percentile(
            [
                float(item["latency_ms"])
                for item in items
                if isinstance(item.get("latency_ms"), (int, float))
            ],
            0.95,
        ),
        "required_target_recall": _mean_numeric(
            item.get("target_term_recall") for item in required
        ),
        "irrelevant_seed_mention_rate": _rate(
            bool(item.get("seed_term_mentions")) for item in irrelevant
        ),
        "categories": {
            category: _summarize_category(
                [item for item in items if item.get("category") == category]
            )
            for category in sorted(_CATEGORIES)
        },
    }


def _summarize_category(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "prompt_count": len(items),
        "rag_trigger_rate": _rate(
            bool(item.get("rag_triggered")) for item in items
        ),
        "prompt_tokens_mean": _mean_numeric(
            item.get("prompt_tokens") for item in items
        ),
        "latency_ms_mean": _mean_numeric(
            item.get("latency_ms") for item in items
        ),
        "target_term_recall": _mean_numeric(
            item.get("target_term_recall") for item in items
        ),
    }


def _load_results(workspace: EvaluationWorkspace) -> list[dict[str, Any]]:
    results = []
    root = workspace.results_dir / "dialogue_ab"
    for path in sorted(root.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to read dialogue A/B result: %s", path)
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


def _completed_result_keys(
    workspace: EvaluationWorkspace,
) -> set[tuple[str, str]]:
    return {
        (str(item["policy"]), str(item["prompt_id"]))
        for item in _load_results(workspace)
        if item.get("policy") in POLICIES and item.get("prompt_id")
    }


def _result_path(
    workspace: EvaluationWorkspace,
    policy: str,
    prompt_id: str,
) -> Path:
    return (
        workspace.results_dir
        / "dialogue_ab"
        / safe_artifact_name(policy)
        / f"{safe_artifact_name(prompt_id)}.json"
    )


def _checkpoint_path(workspace: EvaluationWorkspace) -> Path:
    return workspace.checkpoints_dir / _CHECKPOINT_NAME


def _load_checkpoint(workspace: EvaluationWorkspace) -> dict[str, Any]:
    path = _checkpoint_path(workspace)
    if not path.is_file():
        return {
            "schema_version": 1,
            "run_id": workspace.run_id,
            "status": "running",
            "seed_completed": False,
        }
    payload = _read_json(path)
    if payload.get("run_id") != workspace.run_id:
        raise DialogueABError("dialogue A/B checkpoint run_id mismatch")
    return payload


def _write_checkpoint(
    workspace: EvaluationWorkspace,
    payload: dict[str, Any],
) -> None:
    write_json(
        _checkpoint_path(workspace),
        {
            **payload,
            "run_id": workspace.run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise DialogueABError(f"unreadable JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise DialogueABError(f"JSON root must be an object: {path}")
    return payload


def _merge_mapping(base: dict[str, Any], overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_mapping(base[key], value)
        else:
            base[key] = value


def _distinctive_seed_terms(dataset: DialogueDataset) -> tuple[str, ...]:
    terms = {
        term
        for prompt in dataset.prompts
        for term in prompt.expected_terms
        if len(_normalize_text(term)) >= 3
    }
    return tuple(sorted(terms, key=lambda item: (-len(item), item)))


def _contains_text(text: Any, term: str) -> bool:
    return _normalize_text(term) in _normalize_text(text)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", text)


def _required_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DialogueABError(f"{context} must be non-empty text")
    return value.strip()


def _optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _safe_id(value: Any, context: str) -> str:
    text = _required_text(value, context)
    if not _SAFE_ID.fullmatch(text):
        raise DialogueABError(
            f"{context} must contain only letters, numbers, '.', '_' or '-'"
        )
    return text


def _text_array(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        raise DialogueABError(f"{context} must be an array")
    return [
        _required_text(item, f"{context}[{index}]")
        for index, item in enumerate(value)
    ]


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _sum_numeric(values: Any) -> Optional[float]:
    numeric = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return sum(numeric) if numeric else None


def _mean_numeric(values: Any) -> Optional[float]:
    numeric = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return statistics.fmean(numeric) if numeric else None


def _numeric_delta(value: Any, baseline: Any) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not isinstance(baseline, (int, float)) or isinstance(baseline, bool):
        return None
    return float(value) - float(baseline)


def _rate(values: Any) -> Optional[float]:
    items = list(values)
    return sum(bool(value) for value in items) / len(items) if items else None


def _percentile(values: list[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile)))
    return ordered[index]


def _emit_progress(progress: float, phase: str, message: str) -> None:
    print(
        f"[DialogueAB {progress:5.1f}%] {phase:<14} | {message}",
        flush=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Japanese production-dialogue policy A/B.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dataset", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--profile", type=Path, required=True)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        config = DialogueABConfig(
            dataset_path=args.dataset,
            output_dir=args.output_dir,
            run_id=args.run_id,
            profile_path=args.profile,
        )
        asyncio.run(run_dialogue_ab(config))
        return 0
    asyncio.run(resume_dialogue_ab(args.run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
