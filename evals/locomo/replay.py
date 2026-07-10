"""Orchestrate the Phase 2 LoCoMo replay, Sleeptime, and minimum QA flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import platform
from pathlib import Path
import re
from typing import Optional

from .adapter import ReplayAdapter
from .artifacts import append_jsonl, snapshot_instance, write_json
from .config import ReplayConfig
from .dataset import LocomoConversation, load_dataset
from .qa_runner import QARunner
from .sleeptime_runner import SleeptimeRunner
from .workspace import EvaluationWorkspace


@dataclass(frozen=True)
class EvaluationRunResult:
    workspace: EvaluationWorkspace
    sample_ids: list[str]
    instance_names: list[str]
    replayed_sessions: int
    answered_questions: int


async def run_evaluation(config: ReplayConfig) -> EvaluationRunResult:
    """Run the Phase 2 pipeline without importing any HTTP or UI layer."""
    conversations = _select_conversations(load_dataset(config.dataset_path), config)
    workspace = EvaluationWorkspace.create(
        config.output_dir,
        run_id=config.run_id,
        clean=config.clean,
    )
    workspace.assert_isolated()
    _write_run_metadata(workspace, config, conversations)

    runtime = workspace.create_runtime()
    sleeptime_runner = SleeptimeRunner(
        workspace.create_sleeptime(),
        run_id=workspace.run_id,
        log_path=workspace.results_dir / "sleeptime_log.jsonl",
    )
    qa_runner = QARunner(
        runtime,
        workspace,
        model_name=config.model_name,
        connection=config.connection,
    )

    instance_names = []
    replayed_sessions = 0
    answered_questions = 0
    for conversation in conversations:
        instance_name = _instance_name(conversation.sample_id)
        instance_names.append(instance_name)
        _create_instance(runtime, conversation, instance_name, config)
        components = runtime.get_instance_components(instance_name)
        adapter = ReplayAdapter(components["memory"], conversation)

        sessions = conversation.sessions[: config.session_limit]
        for session in sessions:
            try:
                saved_turns = adapter.replay_session(session)
                replay_record = {
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
                }
                append_jsonl(
                    workspace.results_dir / "replay_log.jsonl", replay_record
                )
            except Exception as exc:
                append_jsonl(
                    workspace.results_dir / "replay_log.jsonl",
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

            snapshot_root = (
                workspace.snapshots_dir / instance_name / session.session_id
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
            replayed_sessions += 1

        for question in conversation.questions[: config.question_limit]:
            await qa_runner.run(
                sample_id=conversation.sample_id,
                instance_name=instance_name,
                question=question,
            )
            answered_questions += 1

    return EvaluationRunResult(
        workspace=workspace,
        sample_ids=[conversation.sample_id for conversation in conversations],
        instance_names=instance_names,
        replayed_sessions=replayed_sessions,
        answered_questions=answered_questions,
    )


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


def _create_instance(runtime, conversation, instance_name, config) -> None:
    template = (
        f"You are {conversation.speaker_b}, a conversational companion. "
        "Answer evaluation questions from the memories available to you. "
        "When the memories do not contain the answer, say that no information "
        "is available. Do not use external web knowledge."
    )
    success, message = runtime.instance_manager.create_instance(
        instance_name,
        template,
        agent_profile={"ai_name": conversation.speaker_b, "locale": "en"},
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
            "schema_version": 1,
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


def _instance_name(sample_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", sample_id).strip("_").lower()
    if not slug:
        raise ValueError(f"sample_id cannot form an instance name: {sample_id!r}")
    return f"locomo_{slug}"
