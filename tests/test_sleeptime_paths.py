"""ButlySleeptime の隔離パス注入に対する回帰テスト。"""

import json
from pathlib import Path

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
