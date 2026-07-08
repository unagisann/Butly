"""
turn_meta.py
------------
会話ターン message の話者帰属メタ (person_id / display_name / lane / source /
channel_key) の読み出しヘルパ (docs/planning/active/group_context_lanes_plan.ja.md §2.5)。

message 形式::

    {"role": "user", "parts": ["..."],
     "meta": {"person_id": "p_discord_123", "display_name": "たろう",
              "lane": "direct", "source": "discord",
              "channel_key": "guild_id:channel_id"}}

後方互換規則: ``meta`` が無い message は person_id=owner / lane=direct /
source=web とみなす。過去ログ・Web チャットはこの規則で正しく解釈されるため
マイグレーションは不要。
"""

from typing import Any, Dict, List

# persons.json 未整備でも成立する owner の既定 person_id
# (butly_core.external.person_registry.OWNER_FALLBACK_PERSON_ID と同値)。
OWNER_PERSON_ID = "p_owner"

# 既知 lane。audience は将来の実装のための予約のみ (実装しない)。
KNOWN_LANES = ("direct", "ambient", "audience")

# 未知 lane のフォールバック値。読み手は direct 相当で安全側に倒す
# (enum 最小化 + other フォールバックの流儀)。
LANE_OTHER = "other"


def normalize_lane(value: Any) -> str:
    """lane 値を正規化する。欠落は direct、未知値は other。"""
    if not isinstance(value, str) or not value.strip():
        return "direct"
    lane = value.strip().lower()
    return lane if lane in KNOWN_LANES else LANE_OTHER


def _raw_meta(msg: dict) -> Dict[str, Any]:
    meta = msg.get("meta")
    return meta if isinstance(meta, dict) else {}


def effective_meta(
    msg: dict, *, owner_person_id: str = OWNER_PERSON_ID
) -> Dict[str, Any]:
    """後方互換規則を適用した meta を返す (meta 欠落 = owner/direct/web)。"""
    meta = _raw_meta(msg)
    return {
        "person_id": meta.get("person_id") or owner_person_id,
        "display_name": meta.get("display_name") or None,
        "lane": normalize_lane(meta.get("lane")),
        "source": meta.get("source") or "web",
        "channel_key": meta.get("channel_key") or None,
    }


def has_multiple_speakers(
    messages: List[dict], *, owner_person_id: str = OWNER_PERSON_ID
) -> bool:
    """user メッセージに 2 人以上の話者 (person_id) が存在するか。

    meta 欠落 message は owner の発言と解釈する。model / assistant は数えない。
    """
    seen = set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        seen.add(effective_meta(msg, owner_person_id=owner_person_id)["person_id"])
        if len(seen) > 1:
            return True
    return False


def speaker_label(msg: dict, default_user_name: str) -> str:
    """user メッセージの話者表示名 (meta.display_name → 既定呼称の順)。"""
    display = _raw_meta(msg).get("display_name")
    if isinstance(display, str) and display.strip():
        return display.strip()
    return default_user_name


def user_label(msg: dict, default_user_name: str, *, multi_speaker: bool) -> str:
    """整形時の user ラベル。複数話者の場合のみ ``「display_name」`` 形式。

    1:1 (オーナーのみ) は従来どおり既定呼称のまま。既存プロンプトの挙動を
    変えないため、複数話者が実在するときだけプレフィックスを切り替える
    (group_context_lanes_plan §4.3)。
    """
    if not multi_speaker:
        return default_user_name
    return f"「{speaker_label(msg, default_user_name)}」"


def message_text(msg: dict) -> str:
    """message から本文テキストを取り出す (parts[0] が str / dict 両対応)。"""
    parts = msg.get("parts") or []
    content = parts[0] if parts else ""
    if isinstance(content, dict):
        content = content.get("text", "")
    return content if isinstance(content, str) else ""
