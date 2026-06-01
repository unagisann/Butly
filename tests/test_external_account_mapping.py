"""
test_external_account_mapping.py
--------------------------------
ExternalAccountStore（Discord instance 解決・bind/unbind）の単体テスト。
discord.py に依存しない純粋ロジックなので SDK 不在でも実行できる。
"""

import json
from pathlib import Path

import pytest

from butly_core.external.account_mapping import (
    ExternalAccountStore,
    FALLBACK_DEFAULT_INSTANCE,
)


@pytest.fixture
def dirs(tmp_path: Path):
    """data_dir と instances_dir（Butly / Jarvis を実在させる）を返す。"""
    data_dir = tmp_path
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)
    (instances_dir / "Jarvis").mkdir(parents=True)
    return data_dir, instances_dir


@pytest.fixture
def store(dirs):
    data_dir, instances_dir = dirs
    return ExternalAccountStore(data_dir, instances_dir)


def _write_accounts(data_dir: Path, payload: dict):
    (data_dir / "external_accounts.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ===================================================================
# 解決: default
# ===================================================================

class TestDefaultResolution:
    def test_no_file_resolves_to_fallback_default(self, store):
        resolved = store.resolve_discord(
            user_id="1", guild_id="2", channel_id="3"
        )
        assert resolved is not None
        assert resolved.instance_name == FALLBACK_DEFAULT_INSTANCE  # "Butly"
        assert resolved.scope == "default"
        assert resolved.exists is True

    def test_explicit_default_used(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"default_instance": "Jarvis", "discord": {}})
        resolved = store.resolve_discord(user_id="1", guild_id="2", channel_id="3")
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "default"

    def test_null_default_rejects(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"default_instance": None, "discord": {}})
        resolved = store.resolve_discord(user_id="1", guild_id="2", channel_id="3")
        assert resolved is None

    def test_default_to_missing_instance_marks_exists_false(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"default_instance": "Ghost", "discord": {}})
        resolved = store.resolve_discord(user_id="1", guild_id="2", channel_id="3")
        assert resolved.instance_name == "Ghost"
        assert resolved.exists is False


# ===================================================================
# 解決: scope 優先順位
# ===================================================================

class TestScopePriority:
    def test_user_over_channel_and_guild(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(
            data_dir,
            {
                "default_instance": "Butly",
                "discord": {
                    "user:100": "Jarvis",
                    "channel:200:300": "Butly",
                    "guild:200": "Butly",
                },
            },
        )
        resolved = store.resolve_discord(
            user_id="100", guild_id="200", channel_id="300"
        )
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "user"

    def test_channel_over_guild(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(
            data_dir,
            {
                "discord": {
                    "channel:200:300": "Jarvis",
                    "guild:200": "Butly",
                }
            },
        )
        resolved = store.resolve_discord(
            user_id="999", guild_id="200", channel_id="300"
        )
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "channel"

    def test_guild_fallback(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"discord": {"guild:200": "Jarvis"}})
        resolved = store.resolve_discord(
            user_id="999", guild_id="200", channel_id="888"
        )
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "guild"

    def test_dm_skips_channel_and_guild_uses_user(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"discord": {"user:100": "Jarvis"}})
        # DM: guild_id None
        resolved = store.resolve_discord(
            user_id="100", guild_id=None, channel_id="300"
        )
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "user"


# ===================================================================
# bind / unbind
# ===================================================================

class TestBindUnbind:
    def test_bind_user_persists(self, store, dirs):
        data_dir, _ = dirs
        key = store.bind_discord(scope="user", instance_name="Jarvis", user_id="100")
        assert key == "user:100"

        saved = json.loads((data_dir / "external_accounts.json").read_text())
        assert saved["discord"]["user:100"] == "Jarvis"

    def test_bind_channel_persists(self, store, dirs):
        data_dir, _ = dirs
        key = store.bind_discord(
            scope="channel", instance_name="Butly", guild_id="200", channel_id="300"
        )
        assert key == "channel:200:300"
        saved = json.loads((data_dir / "external_accounts.json").read_text())
        assert saved["discord"]["channel:200:300"] == "Butly"

    def test_bind_nonexistent_instance_raises(self, store):
        with pytest.raises(ValueError) as exc:
            store.bind_discord(scope="user", instance_name="Ghost", user_id="100")
        assert "Ghost" in str(exc.value)

    def test_bind_channel_without_guild_raises(self, store):
        with pytest.raises(ValueError):
            store.bind_discord(
                scope="channel", instance_name="Butly", channel_id="300"
            )

    def test_bind_unknown_scope_raises(self, store):
        with pytest.raises(ValueError):
            store.bind_discord(scope="weird", instance_name="Butly", user_id="1")

    def test_unbind_removes(self, store):
        store.bind_discord(scope="user", instance_name="Jarvis", user_id="100")
        assert store.unbind_discord(scope="user", user_id="100") is True
        # 解決すると default に戻る
        resolved = store.resolve_discord(user_id="100", guild_id=None, channel_id="1")
        assert resolved.instance_name == FALLBACK_DEFAULT_INSTANCE

    def test_unbind_absent_returns_false(self, store):
        assert store.unbind_discord(scope="user", user_id="404") is False

    def test_bind_then_resolve(self, store):
        store.bind_discord(
            scope="channel", instance_name="Jarvis", guild_id="200", channel_id="300"
        )
        resolved = store.resolve_discord(
            user_id="1", guild_id="200", channel_id="300"
        )
        assert resolved.instance_name == "Jarvis"
        assert resolved.scope == "channel"


# ===================================================================
# instance ヘルパー
# ===================================================================

class TestInstanceHelpers:
    def test_available_instances_sorted(self, store):
        assert store.available_instances() == ["Butly", "Jarvis"]

    def test_instance_exists(self, store):
        assert store.instance_exists("Butly") is True
        assert store.instance_exists("Ghost") is False
        assert store.instance_exists("") is False

    def test_corrupt_file_falls_back(self, store, dirs):
        data_dir, _ = dirs
        (data_dir / "external_accounts.json").write_text("{ not json", encoding="utf-8")
        resolved = store.resolve_discord(user_id="1", guild_id=None, channel_id="1")
        assert resolved.instance_name == FALLBACK_DEFAULT_INSTANCE

    def test_non_dict_discord_mapping_falls_back(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"discord": ["not", "a", "dict"]})
        resolved = store.resolve_discord(user_id="1", guild_id="2", channel_id="3")
        assert resolved.instance_name == FALLBACK_DEFAULT_INSTANCE
        assert resolved.scope == "default"

    def test_invalid_default_type_falls_back(self, store, dirs):
        data_dir, _ = dirs
        _write_accounts(data_dir, {"default_instance": ["not", "a", "string"]})
        resolved = store.resolve_discord(user_id="1", guild_id=None, channel_id="1")
        assert resolved.instance_name == FALLBACK_DEFAULT_INSTANCE
