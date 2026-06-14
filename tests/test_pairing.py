import json
from datetime import datetime, timedelta, timezone

import pytest

from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.external.pairing import PairingStore


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 14, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def stores(tmp_path):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)
    (instances_dir / "Jarvis").mkdir()
    account_store = ExternalAccountStore(tmp_path, instances_dir)
    clock = Clock()
    pairing_store = PairingStore(
        tmp_path, account_store, ttl_seconds=600, clock=clock
    )
    return account_store, pairing_store, clock


def test_issue_reuses_active_code_for_same_user(stores):
    _, pairing_store, _ = stores
    first = pairing_store.issue(source="line", external_user_id="user-1")
    second = pairing_store.issue(source="line", external_user_id="user-1")
    other = pairing_store.issue(source="line", external_user_id="user-2")

    assert first.code == second.code
    assert first.code != other.code
    assert len(pairing_store.pending()) == 2


def test_approve_binds_line_user_and_removes_pending(stores):
    account_store, pairing_store, _ = stores
    pending = pairing_store.issue(source="line", external_user_id="user-1")

    pairing_store.approve(pending.code, "Jarvis")

    resolved = account_store.resolve_line(user_id="user-1")
    assert resolved.instance_name == "Jarvis"
    assert resolved.scope == "user"
    assert pairing_store.pending() == []

    saved = json.loads((account_store.data_dir / "external_accounts.json").read_text())
    assert saved["line"]["user:user-1"] == "Jarvis"


def test_approve_uses_default_instance(stores):
    account_store, pairing_store, _ = stores
    pending = pairing_store.issue(source="line", external_user_id="user-1")
    pairing_store.approve(pending.code)
    assert account_store.resolve_line(user_id="user-1").instance_name == "Butly"


def test_expired_code_is_removed_and_cannot_be_approved(stores):
    _, pairing_store, clock = stores
    pending = pairing_store.issue(source="line", external_user_id="user-1")
    clock.now += timedelta(minutes=11)

    assert pairing_store.pending() == []
    with pytest.raises(KeyError):
        pairing_store.approve(pending.code, "Butly")


def test_reject_removes_pending(stores):
    _, pairing_store, _ = stores
    pending = pairing_store.issue(source="line", external_user_id="user-1")
    pairing_store.reject(pending.code)
    assert pairing_store.pending() == []


def test_resolve_line_prefers_group_specific_binding(tmp_path):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)
    (instances_dir / "Jarvis").mkdir()
    store = ExternalAccountStore(tmp_path, instances_dir)
    (tmp_path / "external_accounts.json").write_text(
        json.dumps(
            {
                "line": {
                    "user:user-1": "Butly",
                    "group:group-1:user-1": "Jarvis",
                }
            }
        )
    )
    assert (
        store.resolve_line(user_id="user-1", group_id="group-1").instance_name
        == "Jarvis"
    )
    assert store.resolve_line(user_id="unknown") is None
