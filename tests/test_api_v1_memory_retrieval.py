"""Contract tests for the versioned memory-retrieval settings resources."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from butly_api import create_app
from butly_api.context import ApiContext
from butly_core import config as legacy_config
from butly_core.settings import clear_settings_cache


@pytest.fixture(autouse=True)
def restore_runtime_settings():
    snapshot = deepcopy(legacy_config.SYSTEM_CONFIG)
    yield
    legacy_config.SYSTEM_CONFIG.clear()
    legacy_config.SYSTEM_CONFIG.update(snapshot)
    clear_settings_cache()


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    instances_dir = tmp_path / "instances"
    instance_dir = instances_dir / "alpha"
    instance_dir.mkdir(parents=True)
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: object(),
        settings_loaded=True,
    )
    return TestClient(create_app(context=context)), tmp_path, instance_dir


def test_global_get_returns_defaults_and_origins(client):
    api, _data_dir, _instance_dir = client
    response = api.get("/api/v1/settings/memory-retrieval")

    assert response.status_code == 200
    body = response.json()
    assert body["defaults"]["evidence_fusion_base_weight"] == 0.7
    assert body["effective"]["search_mode"] == "vector"
    assert body["global_override"] == {}
    assert set(body["origins"].values()) == {"default"}


def test_global_patch_preserves_zero_unrelated_sections_and_updates_runtime(client):
    api, data_dir, _instance_dir = client
    config_path = data_dir / "user_config.json"
    config_path.write_text(
        json.dumps(
            {
                "AI_CONFIG": {"chat": {"model_name": "keep-me"}},
                "SYSTEM_CONFIG": {
                    "trace": {"enabled": True},
                    "memory": {"short_term_limit": 9},
                },
                "PRIVATE_TEST_VALUE": "preserved",
            }
        ),
        encoding="utf-8",
    )

    response = api.patch(
        "/api/v1/settings/memory-retrieval",
        json={
            "evidence_fusion_base_weight": 0.55,
            "rag_raw_max_chars": 0,
            "rag_raw_top_k": 0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective"]["rag_raw_max_chars"] == 0
    assert body["effective"]["rag_raw_top_k"] == 0
    assert body["origins"]["evidence_fusion_base_weight"] == "global"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["AI_CONFIG"]["chat"]["model_name"] == "keep-me"
    assert saved["SYSTEM_CONFIG"]["trace"] == {"enabled": True}
    assert saved["SYSTEM_CONFIG"]["memory"]["short_term_limit"] == 9
    assert saved["PRIVATE_TEST_VALUE"] == "preserved"
    assert legacy_config.SYSTEM_CONFIG["memory"]["rag_raw_max_chars"] == 0


def test_instance_override_precedence_and_null_restores_inheritance(client):
    api, data_dir, instance_dir = client
    (data_dir / "user_config.json").write_text(
        json.dumps(
            {
                "SYSTEM_CONFIG": {
                    "brain": {"evidence_fusion_base_weight": 0.6}
                }
            }
        ),
        encoding="utf-8",
    )
    config_path = instance_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agent_profile": {"ai_name": "Alpha"},
                "brain": {
                    "evidence_fusion_base_weight": 0.4,
                    "readable_instances": ["self"],
                },
            }
        ),
        encoding="utf-8",
    )

    get_response = api.get(
        "/api/v1/instances/alpha/settings/memory-retrieval"
    )
    assert get_response.status_code == 200
    first = get_response.json()
    assert first["global_effective"]["evidence_fusion_base_weight"] == 0.6
    assert first["effective"]["evidence_fusion_base_weight"] == 0.4
    assert first["origins"]["evidence_fusion_base_weight"] == "instance"

    patch_response = api.patch(
        "/api/v1/instances/alpha/settings/memory-retrieval",
        json={
            "evidence_fusion_base_weight": None,
            "rag_raw_neighbor_radius": 1,
        },
    )
    assert patch_response.status_code == 200
    body = patch_response.json()
    assert body["effective"]["evidence_fusion_base_weight"] == 0.6
    assert body["origins"]["evidence_fusion_base_weight"] == "global"
    assert body["effective"]["rag_raw_neighbor_radius"] == 1
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "evidence_fusion_base_weight" not in saved["brain"]
    assert saved["brain"]["readable_instances"] == ["self"]
    assert saved["agent_profile"] == {"ai_name": "Alpha"}


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"search_mode": "unknown"}, "search_mode"),
        ({"rag_raw_max_chars": -1}, "rag_raw_max_chars"),
        ({"rag_raw_top_k": True}, "rag_raw_top_k"),
        ({"unknown_key": 1}, "unknown_key"),
    ],
)
def test_patch_rejects_invalid_or_unknown_values(client, payload, field):
    api, _data_dir, _instance_dir = client
    response = api.patch(
        "/api/v1/settings/memory-retrieval",
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert field in response.text


def test_patch_rejects_non_finite_weight(client):
    api, _data_dir, _instance_dir = client
    response = api.patch(
        "/api/v1/settings/memory-retrieval",
        content=b'{"evidence_fusion_base_weight": NaN}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_patch_rejects_candidate_pool_smaller_than_injection_limit(client):
    api, _data_dir, _instance_dir = client
    response = api.patch(
        "/api/v1/settings/memory-retrieval",
        json={"vector_search_limit": 10, "vector_candidates": 3},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_memory_retrieval_settings"
    assert body["details"]["errors"][0]["field"] == "vector_candidates"


def test_unknown_instance_returns_typed_404(client):
    api, _data_dir, _instance_dir = client
    response = api.get(
        "/api/v1/instances/missing/settings/memory-retrieval"
    )
    assert response.status_code == 404
    assert response.json()["code"] == "instance_not_found"


def test_atomic_write_failure_keeps_existing_instance_config(client, monkeypatch):
    api, _data_dir, instance_dir = client
    config_path = instance_dir / "config.json"
    original = '{"agent_profile":{"ai_name":"Alpha"}}\n'
    config_path.write_text(original, encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(
        "butly_core.settings.memory_retrieval.atomic_write_text",
        fail_write,
    )
    response = api.patch(
        "/api/v1/instances/alpha/settings/memory-retrieval",
        json={"rag_source_mode": "both"},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "settings_persistence_failed"
    assert config_path.read_text(encoding="utf-8") == original
