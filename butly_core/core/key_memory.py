"""
key_memory.py
-------------
Key Memory の YAML 読み書き + LLM 形式変換 + ID 採番ユーティリティ。
提案のパース・解決・適用。
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


YAML_FILENAME = "Key_Memory.yaml"
TXT_FILENAME = "Key_Memory.txt"
PROPOSALS_FILENAME = "key_memory_proposals.json"

logger = logging.getLogger(__name__)


def load_yaml(yaml_path: Path) -> list[dict]:
    """Key_Memory.yaml を読み込んで entries リストを返す。"""
    if not yaml_path.exists():
        return []
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "entries" not in data:
        return []
    return data["entries"]


def save_yaml(entries: list[dict], yaml_path: Path) -> None:
    """entries リスト → Key_Memory.yaml 書き出し。ID 重複チェック付き。"""
    ids = [e["id"] for e in entries if "id" in e]
    if len(ids) != len(set(ids)):
        duplicates = [i for i in ids if ids.count(i) > 1]
        raise ValueError(f"Duplicate entry IDs found: {set(duplicates)}")
    data = {"version": 1, "entries": entries}
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def next_id(entries: list[dict]) -> str:
    """既存エントリから次の ID を自動採番。"""
    nums = [
        int(e["id"].split("_")[1])
        for e in entries
        if e.get("id", "").startswith("km_") and e["id"].split("_")[1].isdigit()
    ]
    return f"km_{(max(nums, default=0) + 1):03d}"


def yaml_to_text(entries: list[dict], mode: str = "high") -> str:
    """entries → コンテキスト注入用テキスト。mode: high / low"""
    if not entries:
        return ""

    if mode == "low":
        items = [e["content"] for e in entries]
        truncated = [c[:20] for c in items[:5]]
        return "[会話核] " + " / ".join(truncated)

    # high モード: target ごとにセクション分け
    human_entries = [e for e in entries if e.get("target") == "human"]
    persona_entries = [e for e in entries if e.get("target") == "persona"]

    parts = []
    if human_entries:
        lines = ["[About User]"]
        for e in human_entries:
            lines.append(f"- {e['content']}")
        parts.append("\n".join(lines))
    if persona_entries:
        lines = ["[About Me]"]
        for e in persona_entries:
            lines.append(f"- {e['content']}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def yaml_to_llm_format(entries: list[dict]) -> str:
    """entries → [target] content 形式（LLM 入力用）。"""
    lines = []
    for e in entries:
        target = e.get("target", "human")
        lines.append(f"[{target}] {e['content']}")
    return "\n".join(lines)


# ==========================================
# 提案のパース・解決・適用
# ==========================================

def parse_proposals(llm_output: str, entries: list[dict]) -> list[dict]:
    """LLM 出力テキストから ADD / REMOVE / UPDATE 提案をパースする。

    Returns:
        list[dict]: 各要素は以下のキーを持つ:
            action: "add" | "remove" | "update"
            target: "human" | "persona"
            content: str          (add/remove 時)
            old_content: str      (update 時)
            new_content: str      (update 時)
            reason: str
            entry_id: str | None  (remove/update 時に resolve)
    """
    text = llm_output.strip()
    if not text or "NO_PROPOSALS" in text:
        return []

    proposals: list[dict] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # ADD [target] content
        m_add = re.match(r"^ADD\s+\[(\w+)\]\s+(.+)$", line)
        if m_add:
            target, content = m_add.group(1), m_add.group(2)
            reason = _extract_reason(lines, i + 1)
            proposals.append({
                "action": "add",
                "target": target,
                "content": content,
                "reason": reason,
                "entry_id": None,
            })
            i += 2 if reason else 1
            continue

        # REMOVE [target] content
        m_rm = re.match(r"^REMOVE\s+\[(\w+)\]\s+(.+)$", line)
        if m_rm:
            target, content = m_rm.group(1), m_rm.group(2)
            reason = _extract_reason(lines, i + 1)
            eid = resolve_entry_id(entries, target, content)
            proposals.append({
                "action": "remove",
                "target": target,
                "content": content,
                "reason": reason,
                "entry_id": eid,
            })
            i += 2 if reason else 1
            continue

        # UPDATE [target] old → new
        m_up = re.match(r"^UPDATE\s+\[(\w+)\]\s+(.+?)\s*→\s*(.+)$", line)
        if m_up:
            target = m_up.group(1)
            old_content = m_up.group(2).strip()
            new_content = m_up.group(3).strip()
            reason = _extract_reason(lines, i + 1)
            eid = resolve_entry_id(entries, target, old_content)
            proposals.append({
                "action": "update",
                "target": target,
                "old_content": old_content,
                "new_content": new_content,
                "reason": reason,
                "entry_id": eid,
            })
            i += 2 if reason else 1
            continue

        i += 1

    return proposals


def _extract_reason(lines: list[str], idx: int) -> str:
    """REASON: 行があれば理由テキストを返す。"""
    if idx < len(lines):
        m = re.match(r"^REASON:\s*(.+)$", lines[idx].strip())
        if m:
            return m.group(1)
    return ""


def resolve_entry_id(
    entries: list[dict], target: str, content: str
) -> Optional[str]:
    """content と target から既存エントリの ID を特定する。

    1. 完全一致
    2. 前方一致（content が既存 content の先頭に一致）
    3. 見つからなければ None
    """
    # 完全一致
    for e in entries:
        if e.get("target") == target and e.get("content") == content:
            return e["id"]
    # 前方一致
    for e in entries:
        if e.get("target") == target and e.get("content", "").startswith(content):
            return e["id"]
    return None


def apply_proposal(proposal: dict, yaml_path: Path) -> bool:
    """承認済み提案を YAML ファイルに適用する。

    Returns:
        True: 適用成功
        False: 適用失敗（ID 解決不能など）
    """
    entries = load_yaml(yaml_path)
    action = proposal["action"]

    if action == "add":
        new_entry = {
            "id": next_id(entries),
            "target": proposal["target"],
            "content": proposal.get("content", ""),
        }
        entries.append(new_entry)

    elif action == "remove":
        eid = proposal.get("entry_id")
        if not eid:
            logger.warning("apply_proposal: remove failed — no entry_id")
            return False
        before = len(entries)
        entries = [e for e in entries if e["id"] != eid]
        if len(entries) == before:
            logger.warning("apply_proposal: remove failed — entry_id %s not found", eid)
            return False

    elif action == "update":
        eid = proposal.get("entry_id")
        if not eid:
            logger.warning("apply_proposal: update failed — no entry_id")
            return False
        found = False
        for e in entries:
            if e["id"] == eid:
                e["content"] = proposal.get("new_content", proposal.get("content", ""))
                if "target" in proposal:
                    e["target"] = proposal["target"]
                found = True
                break
        if not found:
            logger.warning("apply_proposal: update failed — entry_id %s not found", eid)
            return False
    else:
        logger.warning("apply_proposal: unknown action %s", action)
        return False

    save_yaml(entries, yaml_path)
    return True


# ==========================================
# 提案ファイル I/O
# ==========================================

def load_proposals(instance_path: Path) -> list[dict]:
    """key_memory_proposals.json を読み込む。"""
    p = instance_path / PROPOSALS_FILENAME
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_proposals(proposals: list[dict], instance_path: Path) -> None:
    """key_memory_proposals.json を書き出す。"""
    p = instance_path / PROPOSALS_FILENAME
    p.write_text(
        json.dumps(proposals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
