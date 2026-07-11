"""ButlySleeptime の隔離パス注入に対する回帰テスト。"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from sleeptime import ButlySleeptime


def _write_turn(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "timestamp": "2023-05-08T13:56:00",
                "messages": [
                    {
                        "role": "user",
                        "parts": ["I started learning pottery."],
                        "meta": {
                            "person_id": "p_eval_caroline",
                            "display_name": "Caroline",
                            "lane": "direct",
                            "source": "eval",
                        },
                    },
                    {"role": "model", "parts": ["That sounds exciting."]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _build_isolated_instance(instances_dir: Path) -> Path:
    instance_dir = instances_dir / "locomo_sample_0"
    (instance_dir / "short_term_json").mkdir(parents=True)
    (instance_dir / "memory_archive" / "1_integrated").mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        json.dumps(
            {
                "agent_profile": {"ai_name": "Melanie"},
                "user_profile": {"preferred_call": "Caroline"},
                "sleeptime": {
                    "update_targets": {
                        "mid_term_digest": False,
                        "recent_headlines": False,
                        "recent_snapshot": False,
                        "key_memory": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _write_turn(
        instance_dir / "short_term_json" / "session_20230508_135600_000000.json"
    )
    return instance_dir


def test_injected_paths_isolate_stage1_and_person_registry(tmp_path):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    instance_dir = _build_isolated_instance(instances_dir)
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )

    sleeptime.stage_1_cleanup(instance_dir.name)

    integrated = instance_dir / "memory_archive" / "1_integrated"
    assert not list((instance_dir / "short_term_json").glob("*.json"))
    assert len(list(integrated.glob("session_*.json"))) == 1
    assert (data_dir / "persons.json").exists()


def test_injected_paths_are_used_by_stage2_and_backup(tmp_path):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    instance_dir = _build_isolated_instance(instances_dir)
    source = instance_dir / "short_term_json" / "session_20230508_135600_000000.json"
    source.unlink()
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )

    sleeptime.stage_2_knowledgeize(instance_dir.name, instance_dir.name)

    database = instance_dir / "butly_memory.db"
    assert database.exists()
    sleeptime.backup_database(instance_dir.name)
    backups = data_dir / "butly_core" / "db_backups"
    assert list(backups.glob(f"{instance_dir.name}_butly_memory_*.db"))


def test_run_enumerates_only_injected_instances(tmp_path, monkeypatch):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    expected = _build_isolated_instance(instances_dir)
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )
    processed = []
    monkeypatch.setattr(sleeptime, "process_instance", processed.append)

    sleeptime.run()

    assert processed == [expected]


def _instance_with_roles(instances_dir: Path, roles: dict) -> Path:
    """user 定義 connection を持つロール設定つきインスタンスを作る。"""
    instance_dir = instances_dir / "locomo_conn"
    (instance_dir / "memory_archive" / "1_integrated").mkdir(parents=True)
    config = {
        "agent_profile": {"ai_name": "Melanie"},
        "user_profile": {"preferred_call": "Caroline"},
    }
    config.update(roles)
    (instance_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    return instance_dir


def test_daily_digest_resolves_provider_with_connection(tmp_path, monkeypatch):
    """summary の provider 取得が model_name 文字列でなく connection 込み
    dict で解決される（user 定義 connection の推定失敗を防ぐ回帰）。"""
    instances_dir = tmp_path / "instances"
    instance_dir = _instance_with_roles(
        instances_dir,
        {"summary": {"connection": "colab_local", "model_name": "qwen3-14b"}},
    )
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )

    created = []

    def fake_create(model):
        created.append(model)
        provider = MagicMock()
        provider.classify.return_value = "digest body"
        return provider

    monkeypatch.setattr(
        "butly_core.llm.factory.ProviderFactory.create", fake_create
    )

    raw_text = "[2023-05-08 13:56] Caroline: I started pottery today.\n" * 30
    sleeptime._generate_daily_digest(instance_dir, raw_text)

    assert created, "provider was never created"
    assert all(isinstance(model, dict) for model in created), created
    assert created[0]["connection"] == "colab_local"
    assert created[0]["model_name"] == "qwen3-14b"


def test_recent_headlines_keeps_connection_in_provider_and_classify(
    tmp_path, monkeypatch
):
    """recent_headlines は provider 取得と classify の両方で connection を保つ
    （classify へ渡す conf を connection 抜きで組み直していたバグの回帰）。"""
    instances_dir = tmp_path / "instances"
    instance_dir = _instance_with_roles(
        instances_dir,
        {"summary": {"connection": "colab_local", "model_name": "qwen3-14b"}},
    )
    (instance_dir / "mid_term_digest.txt").write_text(
        "[2023-05-08] Pottery\n- Caroline started pottery and planned a mug.\n" * 5,
        encoding="utf-8",
    )
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )

    created = []
    classify_confs = []

    def fake_create(model):
        created.append(model)
        provider = MagicMock()

        def classify(prompt, conf):
            classify_confs.append(conf)
            return json.dumps({"headlines": []})

        provider.classify.side_effect = classify
        return provider

    monkeypatch.setattr(
        "butly_core.llm.factory.ProviderFactory.create", fake_create
    )

    sleeptime._generate_recent_headlines(instance_dir)

    assert created and isinstance(created[0], dict)
    assert created[0]["connection"] == "colab_local"
    assert classify_confs and classify_confs[0].get("connection") == "colab_local"
