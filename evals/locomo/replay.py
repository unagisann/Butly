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
import shutil
import sqlite3
from typing import Optional

from butly_core.core.card_content import compute_content_hash
from butly_core.core.chronos import CHRONOS_NOW_ENV

from .adapter import ReplayAdapter
from .artifacts import append_jsonl, snapshot_instance, write_json
from .checkpoint import (
    STATUS_COMPLETED,
    STATUS_QA,
    STATUS_REPLAYING,
    Checkpoint,
    SampleProgress,
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
    WorkspaceError,
)


_CANONICAL_SAMPLE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_READABLE_SLUG_LENGTH = 96
_MAX_LEGACY_INSTANCE_NAME_LENGTH = 180
LOCOMO_QA_PROMPT_VERSION = "grounded-memory-v3"


@dataclass(frozen=True)
class EvaluationRunResult:
    workspace: EvaluationWorkspace
    sample_ids: list[str]
    instance_names: list[str]
    replayed_sessions: int
    answered_questions: int
    workflow: str = "full"
    prepared_questions: int = 0


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
    _write_run_metadata(workspace, config, conversations, profile)
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
    if run_config.get("stage3_bootstrap"):
        _assert_stage3_bootstrap_completed(workspace, checkpoint)
    if run_config.get("memory_reused_from_run_id"):
        _assert_reused_run_checkpoint(
            workspace,
            checkpoint,
            conversations,
            config.session_limit,
        )
    return await _execute(
        workspace,
        config,
        conversations,
        checkpoint,
        profile,
        progress_reporter,
    )


async def rerun_qa_from_memory(
    source_run_dir: Path,
    *,
    dataset_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    question_limit: Optional[int] = None,
    all_questions: bool = False,
    qa_mode: str = "independent",
    locale: Optional[str] = None,
    model_name: Optional[str] = None,
    connection: Optional[str] = None,
    profile_path: Optional[Path] = None,
    stage3_bootstrap: bool = False,
    progress_reporter: Optional[ProgressReporter] = None,
) -> EvaluationRunResult:
    """Clone a completed independent run's memory and execute QA only.

    The source run remains untouched. Its canonical instances are clean
    post-Sleeptime baselines because independent QA mutates disposable clones.
    A new profile may override prompt context, retrieval, and model settings in
    the copied instances before any question is asked.

    stage3_bootstrap=True は Stage 3 clone A/B の ON 側（§10.2）。
    コピー直後にカード同一性（id + canonical content hash）を source と照合し、
    一致した場合のみ stage3-bootstrap でキューを drain してから QA へ進む。
    bootstrap 後にもカード集合が不変であることを再検証する。
    """
    source_workspace = EvaluationWorkspace.open(
        source_run_dir,
        create_missing=False,
    )
    source_payload = json.loads(
        source_workspace.run_config_path.read_text(encoding="utf-8")
    )
    source_config = ReplayConfig.from_json_dict(source_payload)
    if source_config.qa_mode != "independent":
        raise WorkspaceError(
            "rerun-qa requires a source run with qa_mode=independent; "
            "sequential QA mutates the canonical post-Sleeptime instance"
        )

    selected_sample_ids = tuple(source_payload.get("selected_sample_ids") or ())
    if not selected_sample_ids:
        selected_sample_ids = source_config.sample_ids
    resolved_question_limit = (
        None
        if all_questions
        else (
            question_limit
            if question_limit is not None
            else source_config.question_limit
        )
    )
    config = replace(
        source_config,
        dataset_path=(
            Path(dataset_path)
            if dataset_path is not None
            else source_config.dataset_path
        ),
        output_dir=(
            Path(output_dir)
            if output_dir is not None
            else source_workspace.run_dir.parent
        ),
        run_id=run_id,
        sample_ids=selected_sample_ids,
        sample_limit=None if selected_sample_ids else source_config.sample_limit,
        question_limit=resolved_question_limit,
        qa_mode=qa_mode,
        # A retrieval-prep source is a valid canonical post-Sleeptime memory
        # baseline, but rerun-qa must always execute the full QA workflow.
        workflow="full",
        locale=(
            locale
            if locale is not None
            else (None if profile_path is not None else source_config.locale)
        ),
        model_name=(
            model_name if model_name is not None else source_config.model_name
        ),
        connection=(
            connection if connection is not None else source_config.connection
        ),
        # A copied instance already contains the source profile. Only an
        # explicitly supplied profile should be resolved and overlaid.
        profile_path=Path(profile_path) if profile_path is not None else None,
        clean=False,
    )
    config, profile = _resolve_config_and_profile(config)
    conversations = _select_conversations(load_dataset(config.dataset_path), config)
    _verify_reused_dataset(source_workspace, config.dataset_path)

    source_checkpoint = Checkpoint.load(
        source_workspace.run_id,
        source_workspace.checkpoints_dir,
    )
    for conversation in conversations:
        _validated_reusable_source(
            source_workspace,
            source_checkpoint,
            conversation,
            config.session_limit,
        )
    workspace = EvaluationWorkspace.create(
        config.output_dir,
        run_id=config.run_id,
        clean=False,
    )
    workspace.assert_isolated()
    _write_run_metadata(workspace, config, conversations, profile)
    run_payload = json.loads(
        workspace.run_config_path.read_text(encoding="utf-8")
    )
    run_payload.update(
        {
            "memory_reused_from_run_id": source_workspace.run_id,
            "memory_reused_from_run_dir": str(source_workspace.run_dir),
            "run_sleeptime_per_session": False,
            "stage3_bootstrap": stage3_bootstrap,
        }
    )
    workspace.write_run_config(run_payload)

    checkpoint = Checkpoint.create(workspace.run_id, workspace.checkpoints_dir)
    _copy_reusable_memory(
        source_workspace=source_workspace,
        source_checkpoint=source_checkpoint,
        workspace=workspace,
        checkpoint=checkpoint,
        conversations=conversations,
        session_limit=config.session_limit,
    )
    _copy_memory_build_logs(source_workspace, workspace)

    runtime = workspace.create_runtime()
    for conversation in conversations:
        progress = checkpoint.samples[conversation.sample_id]
        _configure_instance(
            runtime,
            progress.instance_name,
            config,
            profile,
            qa_speaker=conversation.speaker_b,
        )

    # §10.2/§10.3: clone のカード集合が post-Stage 2 正本と完全一致することを
    # QA 前に検証する（OFF/ON 両 clone を同じ正本と照合すれば推移的に同一）。
    identity_report = _verify_clone_card_identity(
        source_workspace=source_workspace,
        workspace=workspace,
        checkpoint=checkpoint,
        conversations=conversations,
    )
    if stage3_bootstrap:
        _bootstrap_stage3_clones(
            workspace=workspace,
            checkpoint=checkpoint,
            conversations=conversations,
            session_limit=config.session_limit,
            identity_report=identity_report,
            progress_reporter=progress_reporter,
        )
    write_json(workspace.run_dir / "card_identity.json", identity_report)

    checkpoint.status = STATUS_QA
    checkpoint.save()
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
            f"workflow={config.workflow}, qa_mode={config.qa_mode}, "
            f"locale={config.locale}"
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
                # Stage 3 の clock は session の元日時を明示注入する
                # (QA 時だけ設定される CHRONOS_NOW_ENV に依存しない)
                session_now=session.timestamp,
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
        if config.workflow == "retrieval_prep":
            continue
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

    if config.workflow == "retrieval_prep":
        _write_retrieval_question_manifest(
            workspace,
            conversations,
            config,
            instance_names_by_sample,
        )
    checkpoint.status = STATUS_COMPLETED
    checkpoint.save()
    _emit_progress(
        progress_reporter,
        progress_total,
        progress_total,
        "evaluation",
        (
            "Replay and Sleeptime completed; retrieval questions prepared"
            if config.workflow == "retrieval_prep"
            else "Replay, Sleeptime, and QA completed"
        ),
    )
    return EvaluationRunResult(
        workspace=workspace,
        sample_ids=[conversation.sample_id for conversation in conversations],
        instance_names=instance_names,
        replayed_sessions=replayed_sessions,
        answered_questions=answered_questions,
        workflow=config.workflow,
        prepared_questions=(
            question_count if config.workflow == "retrieval_prep" else 0
        ),
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
        total += len(sessions) * 2
        if config.workflow == "full":
            total += len(questions)
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
        if config.workflow == "full":
            completed += min(max(progress.qa_completed, 0), len(questions))
    return total, completed


def _write_retrieval_question_manifest(
    workspace: EvaluationWorkspace,
    conversations: list[LocomoConversation],
    config: ReplayConfig,
    instance_names_by_sample: dict[str, str],
) -> None:
    """Persist stable retrieval queries without generating model answers."""
    questions = []
    for conversation in conversations:
        instance_name = instance_names_by_sample[conversation.sample_id]
        for question in conversation.questions[: config.question_limit]:
            questions.append(
                {
                    "run_id": workspace.run_id,
                    "sample_id": conversation.sample_id,
                    "instance_name": instance_name,
                    "question_id": question.question_id,
                    "question": question.question,
                    "expected_answer": question.answer,
                    "category": question.category,
                    "evidence": list(question.evidence),
                }
            )
    write_json(
        workspace.results_dir / "retrieval_questions.json",
        {
            "schema_version": 1,
            "run_id": workspace.run_id,
            "workflow": "retrieval_prep",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_ids": [item.sample_id for item in conversations],
            "question_count": len(questions),
            "questions": questions,
        },
    )


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
    template = _build_qa_system_instruction(conversation.speaker_b)
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

    _configure_instance(
        runtime,
        instance_name,
        config,
        profile,
        qa_speaker=conversation.speaker_b,
    )


def _build_qa_system_instruction(speaker_b: str) -> str:
    """Build the versioned, memory-grounded LoCoMo answer policy.

    Grounding (use every memory section, interpret tense relative to the
    conversation date) is combined with a strict terse-answer format. The
    official LoCoMo scorer is token-F1 over normalized tokens, so a correct
    fact wrapped in a sentence loses precision — v2 dropped the terseness
    clause and Gemini regressed from exact facts to full sentences
    (EM 0.40 -> 0.00). v3 restores the fact-only instruction.
    """
    return (
        f"You are {speaker_b}, a conversational companion. "
        "Answer the evaluation question using only the memory context supplied "
        "with it. All memory sections supplied in the prompt, including "
        "'Past Memories (RAG)', 'Source Conversation Excerpts', and "
        "'Related Matured Memories (active nodes)', count as retrieved "
        "conversation memories. If any of them directly answers the question, "
        "use that information. Memories may describe events from the "
        "perspective of an earlier conversation date. Interpret tense relative "
        "to the original conversation, not the current real-world date. "
        "If the question asks when something happened or was planned, use the "
        "date mentioned for that event, not the memory record's source date or "
        "the current date. "
        # Strict answer format (restored from the pre-v2 prompt):
        "Answer as briefly as possible: give ONLY the fact asked for — such as "
        "a date, a name, or a short phrase — with no explanation, no full "
        "sentence, and no restating of the question. For example, answer "
        "'2022' rather than 'She painted it in 2022.', and 'Single' rather "
        "than 'Caroline is single.'. "
        "Write dates in natural language such as '7 May 2023' (day month "
        "year), never in ISO format like 2023-05-07. "
        "Only answer 'No information available' when none of the supplied "
        "memories contains information that answers the question. "
        "Do not use external web knowledge."
    )


def _merge_profile_section(base: dict, overrides: dict) -> None:
    """profile セクションを再帰マージする。

    `sleeptime.update_targets.knowledge_maturation` だけの上書きで
    digest / knowledge_cards 等の既定を消さないため、浅い update は使わない。
    """
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_profile_section(base[key], value)
        else:
            base[key] = value


def _configure_instance(
    runtime,
    instance_name: str,
    config: ReplayConfig,
    profile: Optional[EvaluationProfile] = None,
    *,
    qa_speaker: Optional[str] = None,
) -> None:
    """Apply evaluation-safe config to a new or copied instance."""

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
            _merge_profile_section(
                instance_config.setdefault(section, {}), overrides
            )
    brain_config = instance_config.setdefault("brain", {})
    if brain_config.get("search_mode") == "hybrid_evidence_fusion":
        # independent QA clones are deleted after each question.  Keep the
        # vector sidecar at run scope so later questions reuse it.
        brain_config.setdefault(
            "evidence_cache_path",
            str(
                Path(runtime.base_dir).parent
                / "retrieval_cache"
                / "evidence_embeddings.sqlite3"
            ),
        )
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
    if qa_speaker is not None:
        updated, update_message = runtime.instance_manager.update_instruction(
            instance_name,
            _build_qa_system_instruction(qa_speaker),
        )
        if not updated:
            raise RuntimeError(
                f"Failed to update evaluation answer prompt: {update_message}"
            )


def _knowledge_card_identity(db_path: Path) -> dict:
    """knowledge_cards の同一性指標（id + §5.2 canonical content hash）を返す。

    保存済み content_hash 列ではなく本文から再計算する。列の欠損・陳腐化が
    あっても、比較は常にカード本文そのものに基づく。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, title, summary, episode, tags, category, source_date
            FROM knowledge_cards
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    cards = [
        {"card_id": row["id"], "content_hash": compute_content_hash(dict(row))}
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(cards, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {"count": len(cards), "digest": digest, "cards": cards}


def _diff_card_identity(source: dict, clone: dict) -> str:
    source_map = {c["card_id"]: c["content_hash"] for c in source["cards"]}
    clone_map = {c["card_id"]: c["content_hash"] for c in clone["cards"]}
    missing = sorted(set(source_map) - set(clone_map))
    extra = sorted(set(clone_map) - set(source_map))
    changed = sorted(
        cid
        for cid in set(source_map) & set(clone_map)
        if source_map[cid] != clone_map[cid]
    )
    return f"missing={missing} extra={extra} changed={changed}"


def _verify_clone_card_identity(
    *,
    source_workspace: EvaluationWorkspace,
    workspace: EvaluationWorkspace,
    checkpoint: Checkpoint,
    conversations: list[LocomoConversation],
) -> dict:
    """clone のカード集合を source と照合し、不一致なら QA 前に失敗終了する。"""
    report: dict = {}
    for conversation in conversations:
        progress = checkpoint.samples[conversation.sample_id]
        source_db = (
            source_workspace.instances_dir
            / progress.instance_name
            / "butly_memory.db"
        )
        clone_db = (
            workspace.instances_dir / progress.instance_name / "butly_memory.db"
        )
        source_identity = _knowledge_card_identity(source_db)
        clone_identity = _knowledge_card_identity(clone_db)
        if source_identity["digest"] != clone_identity["digest"]:
            raise WorkspaceError(
                "Cloned knowledge cards do not match the post-Stage 2 source "
                f"for sample {conversation.sample_id!r}: "
                + _diff_card_identity(source_identity, clone_identity)
            )
        report[conversation.sample_id] = {
            "instance_name": progress.instance_name,
            "card_count": source_identity["count"],
            "digest": source_identity["digest"],
        }
    return report


def _bootstrap_stage3_clones(
    *,
    workspace: EvaluationWorkspace,
    checkpoint: Checkpoint,
    conversations: list[LocomoConversation],
    session_limit: Optional[int],
    identity_report: dict,
    progress_reporter: Optional[ProgressReporter],
) -> None:
    """ON clone で stage3-bootstrap を実行し、カード集合の不変を再検証する。"""
    sleeptime = workspace.create_sleeptime()
    log_path = workspace.results_dir / "stage3_bootstrap_log.jsonl"
    for conversation in conversations:
        progress = checkpoint.samples[conversation.sample_id]
        sessions = conversation.sessions[:session_limit]
        # QA と同じ clock: 最終会話日の翌日（§10.1 の評価 clock 方針）
        reference_now = (
            max(session.timestamp for session in sessions) + timedelta(days=1)
            if sessions
            else None
        )
        _emit_progress(
            progress_reporter,
            0,
            0,
            "stage3-bootstrap",
            f"{conversation.sample_id} draining the review queue",
        )
        # A/B コスト比較用に実測トークンを回収する。事前 pop で前 sample の
        # 取り残しを捨て、bootstrap 後の pop でこの sample 分だけを取り出す。
        sleeptime.pop_llm_usage()
        stats = sleeptime.stage3_bootstrap(
            progress.instance_name, now=reference_now
        )
        usage = sleeptime.pop_llm_usage()
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        append_jsonl(
            log_path,
            {
                "run_id": workspace.run_id,
                "sample_id": conversation.sample_id,
                "instance_name": progress.instance_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                **(stats if isinstance(stats, dict) else {"status": "unknown"}),
            },
        )
        if not isinstance(stats, dict) or stats.get("status") != "completed":
            raise WorkspaceError(
                "stage3-bootstrap did not complete for sample "
                f"{conversation.sample_id!r}: "
                f"{stats.get('status') if isinstance(stats, dict) else stats!r}. "
                "The ON clone is not a valid A/B arm; fix the failure and re-run."
            )

        clone_db = (
            workspace.instances_dir / progress.instance_name / "butly_memory.db"
        )
        post_identity = _knowledge_card_identity(clone_db)
        expected = identity_report[conversation.sample_id]
        if post_identity["digest"] != expected["digest"]:
            raise WorkspaceError(
                "stage3-bootstrap modified knowledge cards for sample "
                f"{conversation.sample_id!r}; the A/B same-cards invariant "
                "is broken"
            )
        sample_report = identity_report[conversation.sample_id]
        sample_report["stage3_bootstrap"] = {
            "status": stats.get("status"),
            "applied_cards": stats.get("applied_cards"),
            "created": stats.get("created"),
            "linked": stats.get("linked"),
            "llm_calls": stats.get("llm_calls"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "post_bootstrap_digest": post_identity["digest"],
        }


def _verify_reused_dataset(
    source_workspace: EvaluationWorkspace,
    dataset_path: Path,
) -> None:
    """Refuse to pair reused memory with a changed dataset file."""
    manifest_path = source_workspace.run_dir / "dataset_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_sha256 = manifest["sha256"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise WorkspaceError(
            "rerun-qa requires a readable source dataset_manifest.json with "
            "a sha256 digest"
        ) from exc
    try:
        actual_sha256 = hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkspaceError(
            f"Could not read the source run's dataset: {dataset_path}"
        ) from exc
    if actual_sha256 != expected_sha256:
        raise WorkspaceError(
            "The dataset file no longer matches the source run's manifest; "
            "refusing to reuse memory with different conversations"
        )


def _copy_reusable_memory(
    *,
    source_workspace: EvaluationWorkspace,
    source_checkpoint: Checkpoint,
    workspace: EvaluationWorkspace,
    checkpoint: Checkpoint,
    conversations: list[LocomoConversation],
    session_limit: Optional[int],
) -> None:
    """Copy clean post-Sleeptime instances and pre-complete memory phases."""
    for conversation in conversations:
        source_progress, source_instance, session_ids = (
            _validated_reusable_source(
                source_workspace,
                source_checkpoint,
                conversation,
                session_limit,
            )
        )

        target_instance = workspace.instances_dir / source_progress.instance_name
        shutil.copytree(source_instance, target_instance)
        for volatile_name in ("debug_logs", "traces"):
            volatile_path = target_instance / volatile_name
            if volatile_path.exists():
                shutil.rmtree(volatile_path)
        (target_instance / "session_state.json").unlink(missing_ok=True)

        target_progress = checkpoint.progress_for(
            conversation.sample_id,
            source_progress.instance_name,
        )
        target_progress.replayed_sessions = list(session_ids)
        target_progress.sleeptime_completed = list(session_ids)
        target_progress.qa_completed = 0

    persons_path = source_workspace.data_dir / "persons.json"
    if persons_path.is_file():
        shutil.copy2(persons_path, workspace.data_dir / persons_path.name)
    checkpoint.save()


def _validated_reusable_source(
    source_workspace: EvaluationWorkspace,
    source_checkpoint: Checkpoint,
    conversation: LocomoConversation,
    session_limit: Optional[int],
) -> tuple[SampleProgress, Path, list[str]]:
    """Resolve one clean source instance and its completed session IDs."""
    source_progress = source_checkpoint.samples.get(conversation.sample_id)
    if source_progress is None:
        raise WorkspaceError(
            "Source checkpoint has no progress for sample "
            f"{conversation.sample_id!r}"
        )
    if not _SAFE_INSTANCE_NAME.fullmatch(source_progress.instance_name):
        raise WorkspaceError(
            "Invalid instance name in source checkpoint: "
            f"{source_progress.instance_name!r}"
        )

    sessions = conversation.sessions[:session_limit]
    session_ids = [session.session_id for session in sessions]
    missing_replay = sorted(
        set(session_ids) - set(source_progress.replayed_sessions)
    )
    missing_sleeptime = sorted(
        set(session_ids) - set(source_progress.sleeptime_completed)
    )
    if missing_replay or missing_sleeptime:
        raise WorkspaceError(
            "Source run has not completed Replay/Sleeptime for sample "
            f"{conversation.sample_id!r}: replay={missing_replay}, "
            f"sleeptime={missing_sleeptime}"
        )

    source_instance = source_workspace.instances_dir / source_progress.instance_name
    if not source_instance.is_dir():
        raise WorkspaceError(f"Source instance not found: {source_instance}")
    short_term_dir = source_instance / "short_term_json"
    if short_term_dir.is_dir() and any(short_term_dir.glob("*.json")):
        raise WorkspaceError(
            "Source canonical instance is not a clean post-Sleeptime "
            f"baseline: {source_instance}"
        )
    return source_progress, source_instance, session_ids


def _assert_reused_run_checkpoint(
    workspace: EvaluationWorkspace,
    checkpoint: Checkpoint,
    conversations: list[LocomoConversation],
    session_limit: Optional[int],
) -> None:
    """Never let resume rebuild memory inside a QA-only reuse run."""
    for conversation in conversations:
        progress = checkpoint.samples.get(conversation.sample_id)
        session_ids = {
            session.session_id
            for session in conversation.sessions[:session_limit]
        }
        if progress is None or not session_ids.issubset(
            set(progress.replayed_sessions)
        ) or not session_ids.issubset(set(progress.sleeptime_completed)):
            raise WorkspaceError(
                "A reused-memory run has an incomplete memory checkpoint; "
                "refusing to execute Replay/Sleeptime during resume"
            )
        if not _SAFE_INSTANCE_NAME.fullmatch(progress.instance_name):
            raise WorkspaceError(
                "A reused-memory run has an invalid checkpoint instance name: "
                f"{progress.instance_name!r}"
            )
        if not (workspace.instances_dir / progress.instance_name).is_dir():
            raise WorkspaceError(
                "A reused-memory run is missing its copied instance: "
                f"{progress.instance_name}"
            )


def _assert_stage3_bootstrap_completed(
    workspace: EvaluationWorkspace,
    checkpoint: Checkpoint,
) -> None:
    """Refuse QA resume unless every Stage 3 clone has a durable completion proof."""
    identity_path = workspace.run_dir / "card_identity.json"
    try:
        identity_report = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "Stage 3 bootstrap completion cannot be verified; refusing to "
            "resume QA from a potentially partial ON clone. Re-run rerun-qa "
            "with a new run ID."
        ) from exc
    if not isinstance(identity_report, dict):
        raise WorkspaceError(
            "Stage 3 bootstrap completion proof is malformed; refusing to "
            "resume QA from a potentially partial ON clone. Re-run rerun-qa "
            "with a new run ID."
        )

    invalid_samples = []
    for sample_id in checkpoint.samples:
        sample_report = identity_report.get(sample_id, {})
        if not isinstance(sample_report, dict):
            invalid_samples.append(sample_id)
            continue
        bootstrap = sample_report.get("stage3_bootstrap", {})
        if not isinstance(bootstrap, dict):
            invalid_samples.append(sample_id)
            continue
        if (
            bootstrap.get("status") != "completed"
            or not sample_report.get("digest")
            or bootstrap.get("post_bootstrap_digest")
            != sample_report.get("digest")
        ):
            invalid_samples.append(sample_id)
    if invalid_samples:
        raise WorkspaceError(
            "Stage 3 bootstrap completion cannot be verified for samples "
            f"{invalid_samples}; refusing to resume QA from a potentially "
            "partial ON clone. Re-run rerun-qa with a new run ID."
        )


def _copy_memory_build_logs(
    source_workspace: EvaluationWorkspace,
    workspace: EvaluationWorkspace,
) -> None:
    """Keep source Replay/Sleeptime cost and provenance visible to scoring."""
    for file_name in ("replay_log.jsonl", "sleeptime_log.jsonl"):
        source_path = source_workspace.results_dir / file_name
        if source_path.is_file():
            shutil.copy2(source_path, workspace.results_dir / file_name)


def _write_run_metadata(
    workspace: EvaluationWorkspace,
    config: ReplayConfig,
    conversations: list[LocomoConversation],
    profile: Optional[EvaluationProfile] = None,
) -> None:
    judge = None
    if profile is not None and profile.judge is not None:
        from evals.semantic_judge import JudgeConfig

        resolved_judge = JudgeConfig.from_mapping(profile.judge)
        if resolved_judge is not None:
            judge = {
                **resolved_judge.public_dict(),
                "config_signature": resolved_judge.signature(),
            }
    workspace.write_run_config(
        {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **config.to_json_dict(),
            "qa_prompt_version": LOCOMO_QA_PROMPT_VERSION,
            "judge": judge,
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
