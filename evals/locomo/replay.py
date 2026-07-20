"""Orchestrate the LoCoMo replay, Sleeptime, and QA flow with checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
from typing import Optional

from butly_core.core.chronos import CHRONOS_NOW_ENV

from .adapter import ReplayAdapter
from .artifacts import append_jsonl, snapshot_instance, write_json
from .checkpoint import (
    STATUS_COMPLETED,
    STATUS_QA,
    STATUS_REPLAYING,
    Checkpoint,
)
from .config import (
    EvaluationProfile,
    ReplayConfig,
    load_profile,
    resolve_evaluation_locale,
)
from .dataset import LocomoConversation, load_dataset
from .progress import ProgressReporter
from .qa_runner import QARunner
from .sleeptime_runner import SleeptimeRunner
from .workspace import (
    EvaluationWorkspace,
    IndependentQAWorkspace,
    SequentialQARecoveryPoint,
)


_CANONICAL_SAMPLE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_READABLE_SLUG_LENGTH = 96
_MAX_LEGACY_INSTANCE_NAME_LENGTH = 180


@dataclass(frozen=True)
class EvaluationRunResult:
    workspace: EvaluationWorkspace
    sample_ids: list[str]
    instance_names: list[str]
    replayed_sessions: int
    answered_questions: int


async def run_evaluation(
    config: ReplayConfig,
    *,
    progress_reporter: Optional[ProgressReporter] = None,
) -> EvaluationRunResult:
    """Start a fresh run without importing any HTTP or UI layer."""
    config, profile = _resolve_config_and_profile(config)
    conversations = _select_conversations(load_dataset(config.dataset_path), config)
    workspace = EvaluationWorkspace.create(
        config.output_dir,
        run_id=config.run_id,
        clean=config.clean,
    )
    workspace.assert_isolated()
    _write_run_metadata(workspace, config, conversations)
    checkpoint = Checkpoint.create(workspace.run_id, workspace.checkpoints_dir)
    return await _execute(
        workspace,
        config,
        conversations,
        checkpoint,
        profile,
        progress_reporter,
    )


async def resume_evaluation(
    run_dir: Path,
    *,
    progress_reporter: Optional[ProgressReporter] = None,
) -> EvaluationRunResult:
    """Continue an interrupted run from its checkpoint without re-ingesting."""
    workspace = EvaluationWorkspace.open(run_dir)
    run_config = json.loads(
        workspace.run_config_path.read_text(encoding="utf-8")
    )
    config = ReplayConfig.from_json_dict(run_config)
    config, profile = _resolve_config_and_profile(config)
    conversations = _select_conversations(load_dataset(config.dataset_path), config)
    checkpoint = Checkpoint.load(workspace.run_id, workspace.checkpoints_dir)
    return await _execute(
        workspace,
        config,
        conversations,
        checkpoint,
        profile,
        progress_reporter,
    )


async def _execute(
    workspace: EvaluationWorkspace,
    config: ReplayConfig,
    conversations: list[LocomoConversation],
    checkpoint: Checkpoint,
    profile: Optional[EvaluationProfile],
    progress_reporter: Optional[ProgressReporter],
) -> EvaluationRunResult:
    instance_names_by_sample = _resolve_instance_names(conversations, checkpoint)
    progress_total, progress_completed = _evaluation_progress_counts(
        conversations,
        config,
        checkpoint,
        instance_names_by_sample,
    )
    session_count = sum(
        len(conversation.sessions[: config.session_limit])
        for conversation in conversations
    )
    question_count = sum(
        len(conversation.questions[: config.question_limit])
        for conversation in conversations
    )
    _emit_progress(
        progress_reporter,
        progress_completed,
        progress_total,
        "setup",
        (
            f"run={workspace.run_id}; samples={len(conversations)}, "
            f"sessions={session_count}, questions={question_count}, "
            f"qa_mode={config.qa_mode}, locale={config.locale}"
        ),
    )
    runtime = workspace.create_runtime()
    sleeptime_runner = SleeptimeRunner(
        workspace.create_sleeptime(),
        run_id=workspace.run_id,
        log_path=workspace.results_dir / "sleeptime_log.jsonl",
    )
    instance_names = []
    replayed_sessions = 0
    answered_questions = 0
    for sample_index, conversation in enumerate(conversations, start=1):
        instance_name = instance_names_by_sample[conversation.sample_id]
        instance_names.append(instance_name)
        _emit_progress(
            progress_reporter,
            progress_completed,
            progress_total,
            "sample",
            (
                f"{conversation.sample_id} "
                f"({sample_index}/{len(conversations)}) starting"
            ),
        )
        progress = checkpoint.progress_for(conversation.sample_id, instance_name)
        sequential_recovery = SequentialQARecoveryPoint(
            workspace,
            sample_id=conversation.sample_id,
            instance_name=instance_name,
        )
        sequential_recovery.reconcile(
            checkpoint_qa_completed=progress.qa_completed
        )
        if not (workspace.instances_dir / instance_name).exists():
            _create_instance(runtime, conversation, instance_name, config, profile)
        components = runtime.get_instance_components(instance_name)
        adapter = ReplayAdapter(components["memory"], conversation)

        sessions = conversation.sessions[: config.session_limit]
        for session_index, session in enumerate(sessions, start=1):
            if session.session_id in progress.sleeptime_completed:
                continue
            checkpoint.status = STATUS_REPLAYING

            if session.session_id not in progress.replayed_sessions:
                _emit_progress(
                    progress_reporter,
                    progress_completed,
                    progress_total,
                    "replay",
                    (
                        f"{conversation.sample_id} {session.session_id} "
                        f"({session_index}/{len(sessions)}) starting"
                    ),
                )
                _discard_partial_session(
                    workspace.instances_dir / instance_name,
                    conversation.sample_id,
                    session.session_id,
                )
                _replay_session_logged(
                    workspace, conversation, instance_name, session, adapter
                )
                progress.replayed_sessions.append(session.session_id)
                checkpoint.save()
                progress_completed += 1
                _emit_progress(
                    progress_reporter,
                    progress_completed,
                    progress_total,
                    "replay",
                    f"{conversation.sample_id} {session.session_id} completed",
                )

            snapshot_root = (
                workspace.snapshots_dir / instance_name / session.session_id
            )
            _emit_progress(
                progress_reporter,
                progress_completed,
                progress_total,
                "sleeptime",
                (
                    f"{conversation.sample_id} {session.session_id} "
                    f"({session_index}/{len(sessions)}) starting"
                ),
            )
            snapshot_instance(
                workspace.instances_dir / instance_name,
                snapshot_root / "before_sleeptime",
            )
            sleeptime_runner.run(
                sample_id=conversation.sample_id,
                session_id=session.session_id,
                instance_name=instance_name,
            )
            snapshot_instance(
                workspace.instances_dir / instance_name,
                snapshot_root / "after_sleeptime",
            )
            progress.sleeptime_completed.append(session.session_id)
            checkpoint.save()
            replayed_sessions += 1
            progress_completed += 1
            _emit_progress(
                progress_reporter,
                progress_completed,
                progress_total,
                "sleeptime",
                f"{conversation.sample_id} {session.session_id} completed",
            )

        questions = conversation.questions[: config.question_limit]
        if len(questions) > progress.qa_completed:
            checkpoint.status = STATUS_QA
            checkpoint.save()
        # 時間推論の評価妥当性: 会話は過去日時で保存されているため、QA 時の
        # システム時刻(Chronos)を最終会話日の翌日に固定する。実時刻のままだと
        # 数年後を「現在」として注入してしまい、時間質問を歪める。
        prev_now = os.environ.get(CHRONOS_NOW_ENV)
        if questions and sessions:
            reference = max(s.timestamp for s in sessions) + timedelta(days=1)
            os.environ[CHRONOS_NOW_ENV] = reference.isoformat()
        try:
            if config.qa_mode == "independent" and (
                len(questions) > progress.qa_completed
            ):
                canonical_dir = workspace.instances_dir / instance_name
                _emit_progress(
                    progress_reporter,
                    progress_completed,
                    progress_total,
                    "qa-setup",
                    (
                        f"{conversation.sample_id} creating the "
                        "post-Sleeptime baseline clone"
                    ),
                )
                with IndependentQAWorkspace(canonical_dir) as qa_workspace:
                    for index, question in enumerate(questions):
                        if index < progress.qa_completed:
                            continue
                        _emit_progress(
                            progress_reporter,
                            progress_completed,
                            progress_total,
                            "qa",
                            (
                                f"{conversation.sample_id} "
                                f"{question.question_id} "
                                f"({index + 1}/{len(questions)}) "
                                "independent starting"
                            ),
                        )
                        qa_workspace.reset()
                        qa_runner = QARunner(
                            qa_workspace.create_runtime(),
                            workspace,
                            model_name=config.model_name,
                            connection=config.connection,
                            instances_dir=qa_workspace.instances_dir,
                            qa_mode=config.qa_mode,
                        )
                        await qa_runner.run(
                            sample_id=conversation.sample_id,
                            instance_name=instance_name,
                            question=question,
                        )
                        progress.qa_completed = index + 1
                        checkpoint.save()
                        answered_questions += 1
                        progress_completed += 1
                        _emit_progress(
                            progress_reporter,
                            progress_completed,
                            progress_total,
                            "qa",
                            (
                                f"{conversation.sample_id} "
                                f"{question.question_id} completed"
                            ),
                        )
            elif config.qa_mode == "sequential":
                qa_runner = QARunner(
                    runtime,
                    workspace,
                    model_name=config.model_name,
                    connection=config.connection,
                    qa_mode=config.qa_mode,
                )
                for index, question in enumerate(questions):
                    if index < progress.qa_completed:
                        continue
                    _emit_progress(
                        progress_reporter,
                        progress_completed,
                        progress_total,
                        "qa",
                        (
                            f"{conversation.sample_id} "
                            f"{question.question_id} "
                            f"({index + 1}/{len(questions)}) "
                            "sequential starting"
                        ),
                    )
                    sequential_recovery.begin(
                        question_index=index,
                        question_id=question.question_id,
                    )
                    await qa_runner.run(
                        sample_id=conversation.sample_id,
                        instance_name=instance_name,
                        question=question,
                    )
                    progress.qa_completed = index + 1
                    checkpoint.save()
                    sequential_recovery.clear()
                    answered_questions += 1
                    progress_completed += 1
                    _emit_progress(
                        progress_reporter,
                        progress_completed,
                        progress_total,
                        "qa",
                        (
                            f"{conversation.sample_id} "
                            f"{question.question_id} completed"
                        ),
                    )
        finally:
            if prev_now is None:
                os.environ.pop(CHRONOS_NOW_ENV, None)
            else:
                os.environ[CHRONOS_NOW_ENV] = prev_now

    checkpoint.status = STATUS_COMPLETED
    checkpoint.save()
    _emit_progress(
        progress_reporter,
        progress_total,
        progress_total,
        "evaluation",
        "Replay, Sleeptime, and QA completed",
    )
    return EvaluationRunResult(
        workspace=workspace,
        sample_ids=[conversation.sample_id for conversation in conversations],
        instance_names=instance_names,
        replayed_sessions=replayed_sessions,
        answered_questions=answered_questions,
    )


def _evaluation_progress_counts(
    conversations: list[LocomoConversation],
    config: ReplayConfig,
    checkpoint: Checkpoint,
    instance_names_by_sample: dict[str, str],
) -> tuple[int, int]:
    total = 0
    completed = 0
    for conversation in conversations:
        sessions = conversation.sessions[: config.session_limit]
        questions = conversation.questions[: config.question_limit]
        total += len(sessions) * 2 + len(questions)
        progress = checkpoint.progress_for(
            conversation.sample_id,
            instance_names_by_sample[conversation.sample_id],
        )
        completed += sum(
            session.session_id in progress.replayed_sessions
            for session in sessions
        )
        completed += sum(
            session.session_id in progress.sleeptime_completed
            for session in sessions
        )
        completed += min(max(progress.qa_completed, 0), len(questions))
    return total, completed


def _emit_progress(
    reporter: Optional[ProgressReporter],
    completed: int,
    total: int,
    phase: str,
    message: str,
) -> None:
    if reporter is not None:
        reporter.emit_evaluation(completed, total, phase, message)


def _replay_session_logged(
    workspace: EvaluationWorkspace,
    conversation: LocomoConversation,
    instance_name: str,
    session,
    adapter: ReplayAdapter,
) -> None:
    log_path = workspace.results_dir / "replay_log.jsonl"
    try:
        saved_turns = adapter.replay_session(session)
        append_jsonl(
            log_path,
            {
                "run_id": workspace.run_id,
                "sample_id": conversation.sample_id,
                "instance_name": instance_name,
                "session_id": session.session_id,
                "session_timestamp": session.timestamp.isoformat(),
                "source_turn_count": len(session.turns),
                "saved_turn_count": len(saved_turns),
                "saved_files": [item.file_name for item in saved_turns],
                "dialog_ids": [
                    dialog_id
                    for item in saved_turns
                    for dialog_id in item.dialog_ids
                ],
                "speaker_roles": adapter.speaker_roles,
                "status": "succeeded",
                "error": None,
            },
        )
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "run_id": workspace.run_id,
                "sample_id": conversation.sample_id,
                "instance_name": instance_name,
                "session_id": session.session_id,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise


def _discard_partial_session(
    instance_dir: Path,
    sample_id: str,
    session_id: str,
) -> None:
    """Drop short-term turns left behind by an interrupted session replay.

    Sessions are checkpointed only after every turn is saved, so any file
    carrying this session's metadata belongs to an incomplete attempt and
    would be double-ingested by the rerun.
    """
    short_term_dir = instance_dir / "short_term_json"
    if not short_term_dir.is_dir():
        return
    for turn_file in short_term_dir.glob("session_*.json"):
        try:
            payload = json.loads(turn_file.read_text(encoding="utf-8"))
            meta = payload.get("messages", [{}])[0].get("meta", {})
        except (json.JSONDecodeError, OSError, IndexError, AttributeError):
            continue
        if (
            meta.get("locomo_sample_id") == sample_id
            and meta.get("locomo_session_id") == session_id
        ):
            turn_file.unlink()


def _select_conversations(
    conversations: list[LocomoConversation],
    config: ReplayConfig,
) -> list[LocomoConversation]:
    selected = conversations
    if config.sample_ids:
        requested = set(config.sample_ids)
        selected = [item for item in conversations if item.sample_id in requested]
        found = {item.sample_id for item in selected}
        missing = requested - found
        if missing:
            raise ValueError(f"Unknown sample_ids: {sorted(missing)}")
    if config.sample_limit is not None:
        selected = selected[: config.sample_limit]
    if not selected:
        raise ValueError("No LoCoMo conversations selected")
    return selected


def _create_instance(
    runtime,
    conversation,
    instance_name,
    config,
    profile: Optional[EvaluationProfile] = None,
) -> None:
    # 公式LoCoMoプロトコルに合わせ短答を指示する。公式F1は正規化トークンの
    # 重複で採点するため、説明的な長文は正解を含んでいてもprecisionが潰れる。
    # 日付も正規化でISO形式(2023-05-07)が1トークンに潰れるため、正解データと
    # 同じ自然形式(7 May 2023)で答えさせる。
    template = (
        f"You are {conversation.speaker_b}, a conversational companion. "
        "Answer evaluation questions from the memories available to you. "
        "Answer in English, matching the language of the official LoCoMo "
        "questions and reference answers. "
        "Answer as briefly as possible: give only the fact asked for, such as "
        "a date, a name, or a short phrase, with no explanation or context. "
        "Write dates in natural format such as '7 May 2023' (day month year), "
        "never in ISO format like 2023-05-07. "
        "When the memories do not contain the answer, reply exactly "
        "'No information available'. Do not use external web knowledge."
    )
    success, message = runtime.instance_manager.create_instance(
        instance_name,
        template,
        agent_profile={
            "ai_name": conversation.speaker_b,
            "locale": config.locale or "en",
        },
        user_profile={
            "user_name": conversation.speaker_a,
            "preferred_call": conversation.speaker_a,
        },
    )
    if not success:
        raise RuntimeError(f"Failed to create evaluation instance: {message}")

    instance_config = runtime.instance_manager.get_instance_config(instance_name)
    instance_config.setdefault("sleeptime", {}).setdefault("update_targets", {}).update(
        {
            "digest": True,
            "recent_snapshot": True,
            "key_memory": False,
            "knowledge_cards": True,
            "raw_memory_cache": True,
            "knowledge_maturation": False,
        }
    )
    if profile:
        for section, overrides in profile.sections.items():
            instance_config.setdefault(section, {}).update(overrides)
    instance_config.setdefault("prompts", {})["allow_user_overrides"] = False
    if config.model_name is not None:
        instance_config.setdefault("chat", {})["model_name"] = config.model_name
    if config.connection is not None:
        instance_config.setdefault("chat", {})["connection"] = config.connection
    updated, update_message = runtime.instance_manager.update_instance_config(
        instance_name, instance_config
    )
    if not updated:
        raise RuntimeError(
            f"Failed to configure evaluation instance: {update_message}"
        )


def _write_run_metadata(
    workspace: EvaluationWorkspace,
    config: ReplayConfig,
    conversations: list[LocomoConversation],
) -> None:
    workspace.write_run_config(
        {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **config.to_json_dict(),
            "selected_sample_ids": [item.sample_id for item in conversations],
        }
    )
    dataset_path = Path(config.dataset_path)
    write_json(
        workspace.run_dir / "dataset_manifest.json",
        {
            "path": str(dataset_path.resolve()),
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "selected_samples": [
                {
                    "sample_id": item.sample_id,
                    "session_count": len(item.sessions),
                    "question_count": len(item.questions),
                }
                for item in conversations
            ],
            "license": "CC BY-NC 4.0 for the official LoCoMo dataset; "
            "the bundled test fixture is synthetic",
        },
    )
    write_json(
        workspace.run_dir / "environment.json",
        {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
    )


def _resolve_config_and_profile(
    config: ReplayConfig,
) -> tuple[ReplayConfig, Optional[EvaluationProfile]]:
    profile = (
        load_profile(config.profile_path)
        if config.profile_path is not None
        else None
    )
    locale = resolve_evaluation_locale(
        config.locale,
        profile.locale if profile is not None else None,
    )
    return replace(config, locale=locale), profile


def _instance_name(sample_id: str) -> str:
    """Build a readable, deterministic instance name without slug collisions.

    Existing lowercase kebab-case IDs retain their historical names so normal
    runs and their checkpoints keep working. IDs whose spelling would lose
    information during slugging get an explicit hash namespace instead.
    """
    if not isinstance(sample_id, str) or not sample_id:
        raise ValueError(f"Invalid sample_id for instance name: {sample_id!r}")

    slug = re.sub(r"[^A-Za-z0-9]+", "_", sample_id).strip("_").lower()
    legacy_name = f"locomo_{slug}"
    if (
        _CANONICAL_SAMPLE_ID.fullmatch(sample_id)
        and len(legacy_name) <= _MAX_LEGACY_INSTANCE_NAME_LENGTH
    ):
        return legacy_name

    readable_slug = slug[:_MAX_READABLE_SLUG_LENGTH].rstrip("_") or "sample"
    digest = hashlib.sha256(sample_id.encode("utf-8")).hexdigest()
    return f"locomo_{readable_slug}__{digest}"


def _resolve_instance_names(
    conversations: list[LocomoConversation],
    checkpoint: Checkpoint,
) -> dict[str, str]:
    """Resolve names up front and reject corrupt/ambiguous run mappings."""
    resolved: dict[str, str] = {}
    owners: dict[str, str] = {}
    for conversation in conversations:
        sample_id = conversation.sample_id
        previous = checkpoint.samples.get(sample_id)
        instance_name = (
            previous.instance_name
            if previous is not None
            else _instance_name(sample_id)
        )
        if not _SAFE_INSTANCE_NAME.fullmatch(instance_name):
            raise ValueError(
                "Invalid LoCoMo instance name "
                f"{instance_name!r} for sample_id {sample_id!r}; "
                "the checkpoint may be corrupt"
            )
        existing_owner = owners.get(instance_name)
        if existing_owner is not None and existing_owner != sample_id:
            raise ValueError(
                "LoCoMo instance-name collision: sample_ids "
                f"{existing_owner!r} and {sample_id!r} both resolve to "
                f"{instance_name!r}. Start a clean run or repair the "
                "checkpoint before resuming."
            )
        owners[instance_name] = sample_id
        resolved[sample_id] = instance_name
    return resolved
