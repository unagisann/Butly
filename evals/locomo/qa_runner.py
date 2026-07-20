"""Run LoCoMo questions through the existing ButlyRuntime chat path."""

from __future__ import annotations

import time
from typing import Optional

from butly_core.chat.types import ChatRequest
from butly_core.runtime import ButlyRuntime

from .artifacts import (
    append_jsonl,
    copy_latest_trace,
    resolve_retrieved_card_ids,
)
from .dataset import LocomoQuestion
from .workspace import EvaluationWorkspace


def build_qa_request(
    *,
    question: str,
    instance_name: str,
    model_name: Optional[str] = None,
    connection: Optional[str] = None,
) -> ChatRequest:
    """Build the fixed evaluation request policy: RAG on, external search off."""
    return ChatRequest(
        text=question,
        instance_name=instance_name,
        model_name=model_name,
        connection=connection,
        use_rag=True,
        use_google_search=False,
        use_web_search=False,
        source="api",
        metadata={"evaluation": "locomo", "phase": 2},
    )


class QARunner:
    def __init__(
        self,
        runtime: ButlyRuntime,
        workspace: EvaluationWorkspace,
        *,
        model_name: Optional[str] = None,
        connection: Optional[str] = None,
    ):
        self.runtime = runtime
        self.workspace = workspace
        self.model_name = model_name
        self.connection = connection
        self.log_path = workspace.results_dir / "qa_results.jsonl"

    async def run(
        self,
        *,
        sample_id: str,
        instance_name: str,
        question: LocomoQuestion,
    ) -> dict:
        request = build_qa_request(
            question=question.question,
            instance_name=instance_name,
            model_name=self.model_name,
            connection=self.connection,
        )
        started = time.perf_counter()
        response = await self.runtime.chat(request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        instance_dir = self.workspace.instances_dir / instance_name
        copied_trace = copy_latest_trace(
            instance_dir,
            self.workspace.traces_dir,
            question.question_id,
        )
        if copied_trace is None:
            raise RuntimeError(
                f"ButlyRuntime completed QA without a Trace: {question.question_id}"
            )

        debug_info = response.debug_info or {}
        rag = debug_info.get("rag") if isinstance(debug_info.get("rag"), dict) else {}
        rag_results = rag.get("results") if isinstance(rag.get("results"), list) else []
        retrieved_card_ids = resolve_retrieved_card_ids(
            instance_dir / "butly_memory.db",
            [item for item in rag_results if isinstance(item, dict)],
        )
        trace_path = copied_trace.relative_to(self.workspace.run_dir).as_posix()
        result = {
            "run_id": self.workspace.run_id,
            "sample_id": sample_id,
            "instance_name": instance_name,
            "question_id": question.question_id,
            "question": question.question,
            "expected_answer": question.answer,
            "prediction": response.text,
            "category": question.category,
            "evidence": question.evidence,
            "tier": response.tier,
            "need": response.need,
            "refs": response.refs,
            "sources": response.sources,
            "retrieved_card_ids": retrieved_card_ids,
            "latency_ms": latency_ms,
            "trace_path": trace_path,
            "request": {
                "use_rag": request.use_rag,
                "use_google_search": request.use_google_search,
                "use_web_search": request.use_web_search,
                "source": request.source,
                "model_name": request.model_name,
                "connection": request.connection,
            },
            "diagnostics": {
                "gatekeeper": debug_info.get("gatekeeper", {}),
                "rag": rag,
                "timing": debug_info.get("timing", {}),
                "token_usage": debug_info.get("token_usage"),
                "provider": debug_info.get("provider"),
                "model": debug_info.get("model"),
                "connection_id": debug_info.get("connection_id"),
            },
            "error": None,
        }
        append_jsonl(self.log_path, result)
        return result
