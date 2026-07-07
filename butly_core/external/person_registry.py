"""
person_registry.py
------------------
人物レジストリ: 外部プラットフォームのアカウント ``(source, external_user_id)`` を
内部の人物 ID (person_id) に解決・永続化する。

保存先: ``DATA_DIR/persons.json``

フォーマット例::

    {
      "persons": {
        "p_yuki": {
          "display_name": "悠希",
          "is_owner": true,
          "aliases": {
            "discord": ["123456789012345678"],
            "line": ["Uxxxxxxxxxxxxxxxx"]
          },
          "name_variants": ["悠希", "unagisann"]
        },
        "p_discord_9d589f7b1b7c3a11": {"merged_into": "p_yuki"}
      },
      "stats": {
        "p_yuki": {"appearances": 12, "first_seen": "...", "last_seen": "..."}
      }
    }

設計方針 (docs/planning/active/group_context_lanes_plan.ja.md §2):
  - ユーザー名・表示名はキーにしない。人物の同一性は person_id だけが担う。
  - 未登録の外部ユーザーには **決定的な仮 ID** ``p_{source}_{hash}``
    を発行する。外部 ID を RAW ログへ直接露出させず、書き込み不要で毎回
    同じ値になるため、手動登録を待たずに帰属を確保できる。後から正式
    person へマージ可能。
  - マージ (``merge_person``) はレジストリに ``merged_into`` を記録するだけで、
    RAW ログは書き換えない (読み出し時解決 = RAW 不変の原則)。
  - stats は adoption gate (人物カード昇格閾値) 用の登場集計。判断は実データが
    溜まるまで保留し、集計だけを先行して動かす (§4.5)。
  - SDK に依存しない純粋ロジック (account_mapping.py と同じ流儀)。
  - 書き込みは ``io_utils.atomic_write_text`` で原子的に行う。
  - persons.json は外部 ID を含むためローカル管理 (gitignore 対象)。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from butly_core.io_utils import atomic_write_text

# 保存ファイル名（DATA_DIR 直下）
PERSONS_FILENAME = "persons.json"

# persons.json 未整備でも成立する owner の既定 person_id。
# 会話ターンの meta 欠落時は「この owner の direct 発言」と解釈される
# (butly_core.core.turn_meta の後方互換規則)。
OWNER_FALLBACK_PERSON_ID = "p_owner"

# 仮 ID に使えない文字の置換パターン
_PROVISIONAL_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def provisional_person_id(source: str, external_user_id: str) -> str:
    """決定的な仮 person_id を発行する。

    レジストリへの書き込みは行わず、同じ入力からは常に同じ ID が得られる。
    外部 ID は hash 化し、RAW ログや debug 表示に直接出さない。
    """
    src = _PROVISIONAL_UNSAFE.sub("_", str(source).strip().lower()) or "unknown"
    uid = str(external_user_id).strip()
    digest = hashlib.sha256(f"{src}\0{uid}".encode("utf-8")).hexdigest()[:16]
    return f"p_{src}_{digest}"


@dataclass(frozen=True)
class ResolvedPerson:
    """解決結果。

    Attributes
    ----------
    person_id : str
        解決された person_id (merged_into は解決済み)。
    display_name : str | None
        レジストリ登録済みの表示名。未登録 (仮 ID) の場合は None。
        adapter がその時点のスナップショットを持っている場合はそちらを優先すること。
    is_owner : bool
        オーナー本人かどうか。
    provisional : bool
        レジストリ未登録の仮 ID かどうか。
    """

    person_id: str
    display_name: Optional[str]
    is_owner: bool
    provisional: bool


class PersonRegistry:
    """``persons.json`` の読み書きと人物解決を担う。

    読み込みは都度ファイルから行い、外部編集にも追従する
    (個人用ローカル用途なので頻度・競合は無視できる)。
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / PERSONS_FILENAME

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        """JSON を読み込む。無い／壊れている場合は空構造を返す。"""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[PersonRegistry] {self.path} 読み込み失敗、空で続行: {e}")
            return {}
        if not isinstance(data, dict):
            print(f"[PersonRegistry] {self.path} が dict でないため無視")
            return {}
        return data

    def _save(self, data: dict) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        atomic_write_text(self.path, text)

    @staticmethod
    def _persons(data: dict) -> Dict[str, dict]:
        persons = data.get("persons")
        return persons if isinstance(persons, dict) else {}

    # ------------------------------------------------------------------
    # 解決
    # ------------------------------------------------------------------
    @staticmethod
    def _follow_merges(persons: Dict[str, dict], person_id: str) -> str:
        """``merged_into`` チェーンを辿って現行の person_id を返す (ループ安全)。"""
        visited = {person_id}
        current = person_id
        while True:
            entry = persons.get(current)
            if not isinstance(entry, dict):
                return current
            merged_into = entry.get("merged_into")
            if not isinstance(merged_into, str) or not merged_into:
                return current
            if merged_into in visited:
                print(f"[PersonRegistry] merged_into がループしています: {merged_into}")
                return current
            visited.add(merged_into)
            current = merged_into

    def _as_resolved(
        self, persons: Dict[str, dict], person_id: str
    ) -> ResolvedPerson:
        entry = persons.get(person_id)
        entry = entry if isinstance(entry, dict) else {}
        return ResolvedPerson(
            person_id=person_id,
            display_name=entry.get("display_name") or None,
            is_owner=bool(entry.get("is_owner")),
            provisional=person_id not in persons,
        )

    def resolve(self, source: str, external_user_id: str) -> ResolvedPerson:
        """``(source, external_user_id)`` を person に解決する。

        優先順位: aliases 完全一致 → 決定的仮 ID 発行。
        いずれの場合も ``merged_into`` は読み出し時に解決する。
        """
        source_key = str(source).strip().lower()
        uid = str(external_user_id).strip()
        data = self._load()
        persons = self._persons(data)

        for pid, entry in persons.items():
            if not isinstance(entry, dict):
                continue
            aliases = entry.get("aliases")
            ids = aliases.get(source_key) if isinstance(aliases, dict) else None
            if isinstance(ids, list) and uid in {str(x) for x in ids}:
                return self._as_resolved(persons, self._follow_merges(persons, pid))

        # 未登録 → 決定的仮 ID。過去にマージ済みならチェーンを辿って正式 person へ。
        pid = provisional_person_id(source_key, uid)
        return self._as_resolved(persons, self._follow_merges(persons, pid))

    def resolve_person_id(self, person_id: str) -> str:
        """既知の person_id を ``merged_into`` 解決済みの現行 ID に正規化する。"""
        return self._follow_merges(self._persons(self._load()), str(person_id))

    def owner_person_id(self) -> str:
        """``is_owner: true`` の登録 person。未登録なら既定値を返す。"""
        persons = self._persons(self._load())
        for pid, entry in persons.items():
            if isinstance(entry, dict) and entry.get("is_owner"):
                return self._follow_merges(persons, pid)
        return OWNER_FALLBACK_PERSON_ID

    def display_name(self, person_id: str) -> Optional[str]:
        """登録済み表示名を返す (merged_into 解決込み)。未登録なら None。"""
        persons = self._persons(self._load())
        entry = persons.get(self._follow_merges(persons, str(person_id)))
        if isinstance(entry, dict):
            return entry.get("display_name") or None
        return None

    # ------------------------------------------------------------------
    # マージ
    # ------------------------------------------------------------------
    def merge_person(self, from_id: str, to_id: str) -> None:
        """``from_id`` を ``to_id`` に統合する。

        RAW ログは書き換えず、レジストリに ``merged_into`` を記録して
        読み出し時に解決する。aliases は to へ移し、stats (派生集計) は合算する。

        Raises
        ------
        ValueError
            from と to が同一 / to が未登録 / マージがループを作る場合。
        """
        from_id = str(from_id)
        to_id = str(to_id)
        if from_id == to_id:
            raise ValueError("from_id と to_id が同一です")

        data = self._load()
        persons = self._persons(data)
        if to_id not in persons:
            raise ValueError(f"マージ先 person '{to_id}' が登録されていません")
        if self._follow_merges(persons, to_id) == from_id:
            raise ValueError(
                f"'{to_id}' は既に '{from_id}' へマージされています (ループ禁止)"
            )

        from_entry = persons.get(from_id)
        from_entry = dict(from_entry) if isinstance(from_entry, dict) else {}
        to_entry = dict(persons[to_id]) if isinstance(persons[to_id], dict) else {}

        # aliases を to へ移動 (重複除去・順序維持)
        from_aliases = from_entry.pop("aliases", None)
        if isinstance(from_aliases, dict):
            to_aliases = to_entry.get("aliases")
            to_aliases = dict(to_aliases) if isinstance(to_aliases, dict) else {}
            for src, ids in from_aliases.items():
                if not isinstance(ids, list):
                    continue
                merged = list(to_aliases.get(src) or [])
                for i in ids:
                    if i not in merged:
                        merged.append(i)
                to_aliases[src] = merged
            to_entry["aliases"] = to_aliases

        from_entry["merged_into"] = to_id
        persons[from_id] = from_entry
        persons[to_id] = to_entry
        data["persons"] = persons

        # stats は派生集計なのでマージ時に合算してよい (RAW ではない)
        stats = data.get("stats")
        if isinstance(stats, dict) and from_id in stats:
            from_stats = stats.pop(from_id)
            if isinstance(from_stats, dict):
                row = stats.get(to_id)
                row = dict(row) if isinstance(row, dict) else {}
                row["appearances"] = int(row.get("appearances") or 0) + int(
                    from_stats.get("appearances") or 0
                )
                for key, pick in (("first_seen", min), ("last_seen", max)):
                    values = [
                        v
                        for v in (row.get(key), from_stats.get(key))
                        if isinstance(v, str) and v
                    ]
                    if values:
                        row[key] = pick(values)
                stats[to_id] = row
            data["stats"] = stats

        self._save(data)

    # ------------------------------------------------------------------
    # 登場集計 (adoption gate 用。判断は実データ待ちで保留)
    # ------------------------------------------------------------------
    def record_appearances(self, appearances: Dict[str, dict]) -> None:
        """person_id ごとの登場集計を stats に加算する。

        Parameters
        ----------
        appearances : dict
            ``{person_id: {"count": int, "first_seen": iso, "last_seen": iso}}``。
            first_seen / last_seen は無い場合 None でよい。
        """
        if not appearances:
            return
        data = self._load()
        stats = data.get("stats")
        stats = stats if isinstance(stats, dict) else {}

        for raw_pid, inc in appearances.items():
            if not isinstance(inc, dict):
                continue
            pid = self.resolve_person_id(str(raw_pid))
            row = stats.get(pid)
            row = dict(row) if isinstance(row, dict) else {}
            row["appearances"] = int(row.get("appearances") or 0) + int(
                inc.get("count") or 0
            )
            first = inc.get("first_seen")
            if isinstance(first, str) and first:
                current = row.get("first_seen")
                row["first_seen"] = (
                    min(current, first)
                    if isinstance(current, str) and current
                    else first
                )
            last = inc.get("last_seen")
            if isinstance(last, str) and last:
                current = row.get("last_seen")
                row["last_seen"] = (
                    max(current, last) if isinstance(current, str) and current else last
                )
            stats[pid] = row

        data["stats"] = stats
        self._save(data)
