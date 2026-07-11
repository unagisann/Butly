"""ButlyChronos の現在時刻オーバーライド（BUTLY_CHRONOS_NOW）の回帰テスト。

履歴リプレイ評価では過去日時の会話を投入して質問するため、システム時刻を
会話の時系列に合わせて固定できる必要がある。未設定時は実時刻（後方互換）。
"""

from datetime import datetime

from butly_core.core.chronos import ButlyChronos, CHRONOS_NOW_ENV


def test_system_note_uses_env_override(monkeypatch):
    monkeypatch.setenv(CHRONOS_NOW_ENV, "2024-05-21T18:45:00")
    note = ButlyChronos().get_system_note()
    assert "2024-05-21 18:45" in note


def test_system_note_defaults_to_real_now(monkeypatch):
    monkeypatch.delenv(CHRONOS_NOW_ENV, raising=False)
    note = ButlyChronos().get_system_note()
    assert str(datetime.now().year) in note


def test_invalid_override_falls_back_to_real_now(monkeypatch):
    monkeypatch.setenv(CHRONOS_NOW_ENV, "not-a-timestamp")
    note = ButlyChronos().get_system_note()
    assert str(datetime.now().year) in note


def test_delta_text_uses_env_override(monkeypatch):
    monkeypatch.setenv(CHRONOS_NOW_ENV, "2024-05-21T09:00:00")
    delta = ButlyChronos().get_delta_text(datetime(2024, 5, 20, 9, 0, 0))
    assert delta == "1日ぶり"
