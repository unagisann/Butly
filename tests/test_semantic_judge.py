import json

import pytest

import evals.semantic_judge as semantic_judge_module
from evals.semantic_judge import (
    JudgeConfig,
    SemanticJudge,
    SemanticJudgeError,
    build_dialogue_judge_input,
    combined_judge_fingerprint,
    judge_dialogue_pair,
    judge_locomo_answer,
    summarize_dialogue_judgments,
)


def _arm(
    grade="pass",
    *,
    confidence="high",
    contradiction=False,
    memory_use="helpful",
    reason="意味が一致する",
):
    return {
        "grade": grade,
        "confidence": confidence,
        "contradiction": contradiction,
        "memory_use": memory_use,
        "unsupported_claims": [],
        "reason": reason,
    }


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []
        self.calls = 0

    def classify(self, prompt, config):
        self.calls += 1
        self.prompts.append((prompt, config))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def pop_last_token_usage(self):
        return {"prompt_tokens": 10, "completion_tokens": 3}

    def pop_last_completion_metadata(self):
        return {"finish_reason": "stop"}


def _judge(responses, **config_overrides):
    config = JudgeConfig(
        model_name=config_overrides.get("model_name", "judge-model"),
        connection=config_overrides.get("connection", "judge-connection"),
        generation_config=config_overrides.get(
            "generation_config", {"max_output_tokens": 512}
        ),
    )
    provider = FakeProvider(responses)
    return SemanticJudge(config, provider=provider), provider


def _pair_kwargs():
    return {
        "prompt_id": "req_02",
        "category": "memory_required",
        "question": "原因は？",
        "expected_behavior": "記憶から具体的な事実を答える",
        "reference_fact": "APIkey.env を APIKey.env と書いたのが原因",
        "expected_terms": ["APIkey.env", "大文字"],
        "review_point": "大小文字の方向を見る",
        "answers": {
            "intent_gated": "APIKey.env と書いたため失敗した",
            "candidates": "APIkey.env と書いたため失敗した",
        },
    }


def test_semantic_judge_loads_user_connections_before_provider_creation(
    monkeypatch,
):
    events = []
    provider = FakeProvider([])

    monkeypatch.setattr(
        semantic_judge_module,
        "_load_runtime_connections",
        lambda: events.append("connections"),
    )

    def fake_create(config):
        events.append(("provider", config["connection"]))
        return provider

    monkeypatch.setattr(
        semantic_judge_module.ProviderFactory,
        "create",
        fake_create,
    )

    judge = SemanticJudge(
        JudgeConfig("judge-model", connection="nanogpt-sub")
    )

    assert judge.provider is provider
    assert events == ["connections", ("provider", "nanogpt-sub")]


def test_judge_config_forces_zero_temperature_and_validates_output_limit():
    config = JudgeConfig.from_mapping(
        {
            "connection": " nano ",
            "model_name": " gemma ",
            "generation_config": {
                "temperature": 1.7,
                "max_output_tokens": 512,
            },
        }
    )

    assert config.connection == "nano"
    assert config.model_name == "gemma"
    assert config.generation_config["temperature"] == 0.0
    assert config.generation_config["max_output_tokens"] == 512

    with pytest.raises(SemanticJudgeError, match="positive integer"):
        JudgeConfig("gemma", generation_config={"max_output_tokens": 0})


def test_dialogue_judge_strict_json_and_reversed_visible_order():
    pass_one = {"A": _arm("pass"), "B": _arm("fail", reason="逆方向")}
    pass_two = {"A": _arm("fail", reason="逆方向"), "B": _arm("pass")}
    judge, provider = _judge(
        [
            f"```json\n{json.dumps(pass_one, ensure_ascii=False)}\n```",
            json.dumps(pass_two, ensure_ascii=False),
        ]
    )

    result = judge_dialogue_pair(judge, **_pair_kwargs())

    assert provider.calls == 2
    assert result["passes"][0]["visible_order"] == {
        "A": "intent_gated",
        "B": "candidates",
    }
    assert result["passes"][1]["visible_order"] == {
        "A": "candidates",
        "B": "intent_gated",
    }
    assert result["arms"]["intent_gated"]["label"] == "pass"
    assert result["arms"]["candidates"]["label"] == "fail"
    assert result["winner"] == "intent_gated"
    assert result["token_usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 6,
    }
    prompt_text = provider.prompts[0][0]
    assert "単語一致だけを正答根拠にしません" in prompt_text
    assert "日本語か英語か" in prompt_text
    response_format = provider.prompts[0][1]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "butly_dialogue_judge"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["A", "B"]
    assert schema["additionalProperties"] is False


def test_locomo_judge_uses_native_json_schema():
    response = {
        "verdict": "correct",
        "confidence": "high",
        "contradiction": False,
        "missing_critical": False,
        "reason": "意味が一致する",
    }
    judge, provider = _judge([json.dumps(response, ensure_ascii=False)])

    result = judge_locomo_answer(
        judge,
        sample_id="sample-1",
        question_id="q-1",
        category=1,
        question="What is the answer?",
        expected_answer="blue mug",
        prediction="a blue cup",
    )

    assert result["verdict"] == "correct"
    response_format = provider.prompts[0][1]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "butly_locomo_judge"
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == [
        "verdict",
        "confidence",
        "contradiction",
        "missing_critical",
        "reason",
    ]
    assert schema["additionalProperties"] is False


def test_dialogue_judge_rejects_extra_keys_and_retains_raw_response():
    invalid = {
        "A": {**_arm(), "score": 2},
        "B": _arm(),
    }
    raw = json.dumps(invalid, ensure_ascii=False)
    judge, _provider = _judge([raw])

    with pytest.raises(SemanticJudgeError, match="keys mismatch") as caught:
        judge_dialogue_pair(judge, **_pair_kwargs())

    assert caught.value.raw_response == raw
    assert caught.value.token_usage["prompt_tokens"] == 10


def test_provider_error_keeps_available_usage_metadata():
    judge, _provider = _judge([RuntimeError("offline")])

    with pytest.raises(SemanticJudgeError, match="provider call failed") as caught:
        judge_dialogue_pair(judge, **_pair_kwargs())

    assert caught.value.token_usage == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
    }
    assert caught.value.completion_metadata == {"finish_reason": "stop"}


def test_fingerprint_covers_semantic_input_and_model_config():
    kwargs = _pair_kwargs()
    payload = build_dialogue_judge_input(**kwargs)
    first = combined_judge_fingerprint(
        JudgeConfig("one", connection="nano"),
        task="dialogue_ab",
        input_payload=payload,
    )
    changed_answer = build_dialogue_judge_input(
        **{
            **kwargs,
            "answers": {**kwargs["answers"], "candidates": "別の回答"},
        }
    )
    second = combined_judge_fingerprint(
        JudgeConfig("one", connection="nano"),
        task="dialogue_ab",
        input_payload=changed_answer,
    )
    third = combined_judge_fingerprint(
        JudgeConfig("two", connection="nano"),
        task="dialogue_ab",
        input_payload=payload,
    )

    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert first["config_fingerprint"] == second["config_fingerprint"]
    assert first["config_fingerprint"] != third["config_fingerprint"]


def test_partial_aggregate_excludes_errors_from_semantic_scores():
    complete = {
        "status": "complete",
        "prompt_id": "p1",
        "category": "memory_required",
        "model": {"model_name": "judge"},
        "winner": "intent_gated",
        "review_required": False,
        "arms": {
            "intent_gated": {
                "normalized_score": 1.0,
                "label": "pass",
                "contradiction": False,
                "order_disagreement": False,
            },
            "candidates": {
                "normalized_score": 0.0,
                "label": "fail",
                "contradiction": True,
                "order_disagreement": False,
            },
        },
    }
    error = {
        "status": "error",
        "prompt_id": "p2",
        "model": {"model_name": "judge"},
    }

    summary = summarize_dialogue_judgments(
        [complete, error], expected_prompt_count=3
    )

    assert summary["status"] == "partial"
    assert summary["judged_prompt_count"] == 1
    assert summary["error_prompt_count"] == 1
    assert summary["missing_prompt_count"] == 1
    assert summary["coverage"] == pytest.approx(1 / 3)
    assert summary["policies"]["intent_gated"]["pass_rate"] == 1.0
