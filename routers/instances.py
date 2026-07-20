"""
routers/instances.py
────────────────────
インスタンス CRUD + config/prompts + history エンドポイント。
"""

from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel

from butly_core.core.gatekeeper import SessionState
import dependencies as deps

router = APIRouter()


class CreateInstanceRequest(BaseModel):
    name: str
    template: str = "{agent_name} is a helpful AI assistant."
    key_memory: str = ""
    agent_profile: dict = {}
    user_profile: dict = {}


class RenameInstanceRequest(BaseModel):
    new_name: str


@router.get("/instances")
def list_instances():
    """List all available AI instances."""
    if not deps.INSTANCES_DIR.exists():
        return []
    instances = sorted(
        [
            p.name
            for p in deps.INSTANCES_DIR.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ]
    )
    return instances


@router.post("/instances")
def create_instance(request: CreateInstanceRequest):
    """Create a new AI instance."""
    success, message = deps.instance_manager.create_instance(
        request.name,
        request.template,
        key_memory=request.key_memory,
        agent_profile=request.agent_profile or {},
        user_profile=request.user_profile or {},
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message, "name": request.name}


@router.post("/instances/{instance_name}/rename")
def rename_instance(instance_name: str, request: RenameInstanceRequest):
    """Rename an existing AI instance."""
    success, result = deps.instance_manager.rename_instance(
        instance_name, request.new_name
    )
    if not success:
        raise HTTPException(status_code=400, detail=result)

    if instance_name in deps.instance_store:
        del deps.instance_store[instance_name]

    return {"message": "Instance renamed", "new_instance_name": result}


@router.delete("/instances/{instance_name}")
def delete_instance(instance_name: str):
    """Delete an existing AI instance."""
    success, result = deps.instance_manager.delete_instance(instance_name)
    if not success:
        raise HTTPException(status_code=400, detail=result)

    if instance_name in deps.instance_store:
        del deps.instance_store[instance_name]

    return {"message": "Instance deleted", "instance_name": instance_name}


@router.post("/instances/{instance_name}/reload")
def reload_instance(instance_name: str):
    """セッションをリセットする。"""
    if instance_name in deps.instance_store:
        del deps.instance_store[instance_name]

    instance_dir = deps.INSTANCES_DIR / instance_name

    try:
        ss = SessionState(instance_dir)
        ss.reset()
    except Exception:
        pass

    return {"message": f"Session for '{instance_name}' has been reset."}


@router.get("/instances/{instance_name}/config")
def get_instance_config(instance_name: str):
    """Get instance-specific configuration."""
    return deps.instance_manager.get_instance_config(instance_name)


@router.post("/instances/{instance_name}/config")
def update_instance_config(instance_name: str, config: Dict[str, Any] = Body(...)):
    """Update instance-specific configuration."""
    success, message = deps.instance_manager.update_instance_config(
        instance_name, config
    )
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return {
        "message": message,
        "config": deps.instance_manager.get_instance_config(instance_name),
    }


@router.get("/instances/{instance_name}/prompts")
def get_instance_prompts(instance_name: str):
    """インスタンスごとのプロンプトテキストを取得。"""
    result = deps.instance_manager.get_instance_prompts(instance_name)
    if result is None:
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")
    return result


@router.post("/instances/{instance_name}/prompts")
def update_instance_prompts(instance_name: str, data: Dict[str, str] = Body(...)):
    """インスタンスごとのプロンプトテキストを保存。"""
    success, message = deps.instance_manager.update_instance_prompts(
        instance_name, data
    )
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return {"message": message}


@router.get("/history/{instance_name}")
def get_history(instance_name: str, limit: int = 10):
    """Get recent chat history."""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]

    chat_history = memory.load_recent_sessions(limit=limit)
    history_msgs = chat_history[0] if isinstance(chat_history, tuple) else chat_history

    formatted_history = []
    for msg in history_msgs:
        content = msg.get("parts", [""])[0]
        if isinstance(content, dict):
            content = content.get("text", "")
        formatted_history.append({"role": msg.get("role"), "parts": [content]})

    return formatted_history


# ==========================================
# 📖 Glossary (共通言語辞書) エンドポイント
# ==========================================


@router.get("/instances/{instance_name}/glossary")
def get_glossary(instance_name: str):
    """インスタンスの Glossary データを取得する。"""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    return memory.get_glossary_raw()


@router.post("/instances/{instance_name}/glossary")
def update_glossary(instance_name: str, data: Dict[str, Any] = Body(...)):
    """インスタンスの Glossary データを保存する。"""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    success = memory.save_glossary(data)
    if not success:
        raise HTTPException(status_code=500, detail="Glossary の保存に失敗しました。")
    return {"message": "Glossary を保存しました。"}


# ==========================================
# 🔄 RAW Memory Cache エンドポイント
# ==========================================


@router.post("/instances/{instance_name}/rebuild_raw_cache")
def rebuild_raw_cache(instance_name: str, params: dict | None = None):
    """RAW メモリキャッシュを即座に再生成する。

    Body (optional):
        max_tokens: int  — トークン上限 (UI のスライダー値を優先)
        injection_format: str — "plaintext" | "markdown" | "compact"
    """
    from butly_core.config import SYSTEM_CONFIG
    from butly_core.core.raw_memory_reader import build_raw_memory_cache

    instance_dir = deps.INSTANCES_DIR / instance_name
    if not instance_dir.exists():
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")

    params = params or {}

    # インスタンス設定を読み込み（旧 agent → agent_profile/user_profile 変換込み）
    from butly_core.core.memory import _migrate_legacy_agent

    config = _migrate_legacy_agent(
        deps.instance_manager.get_instance_config(instance_name)
    )
    mem_cfg = config.get("memory", {})
    agent_profile = config.get("agent_profile", {})
    user_profile = config.get("user_profile", {})

    # リクエストボディの値を優先、なければ config → SYSTEM_CONFIG
    max_tokens = params.get("max_tokens") or mem_cfg.get(
        "max_raw_tokens", SYSTEM_CONFIG["memory"].get("max_raw_tokens", 4096)
    )
    injection_format = params.get("injection_format") or mem_cfg.get(
        "raw_injection_format",
        SYSTEM_CONFIG["memory"].get("raw_injection_format", "plaintext"),
    )
    agent_name = agent_profile.get("ai_name") or SYSTEM_CONFIG["agent"].get(
        "agent_name", "Agent"
    )
    user_name = (
        user_profile.get("preferred_call")
        or user_profile.get("user_name")
        or SYSTEM_CONFIG["agent"].get("user_name", "User")
    )

    result = build_raw_memory_cache(
        instance_dir,
        max_tokens=max_tokens,
        injection_format=injection_format,
        agent_name=agent_name,
        user_name=user_name,
        locale=agent_profile.get("locale", "ja"),
    )

    return {"status": "ok", **result}


# ==========================================
# 🔑 Key Memory CRUD エンドポイント
# ==========================================


@router.get("/instances/{instance_name}/key_memory")
def get_key_memory(instance_name: str):
    """Key Memory エントリ一覧を返す。"""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    return {"entries": memory.get_key_memory_entries()}


@router.post("/instances/{instance_name}/key_memory")
def add_key_memory(instance_name: str, data: Dict[str, str] = Body(...)):
    """Key Memory にエントリを追加する。ID は自動採番。"""
    from butly_core.core.key_memory import next_id

    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    entries = memory.get_key_memory_entries()

    target = data.get("target", "human")
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content は必須です。")

    new_entry = {"id": next_id(entries), "target": target, "content": content}
    entries.append(new_entry)
    memory.save_key_memory_entries(entries)
    return new_entry


@router.put("/instances/{instance_name}/key_memory/{entry_id}")
def update_key_memory(
    instance_name: str, entry_id: str, data: Dict[str, str] = Body(...)
):
    """指定 ID の Key Memory エントリを更新する。"""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    entries = memory.get_key_memory_entries()

    for e in entries:
        if e["id"] == entry_id:
            if "target" in data:
                e["target"] = data["target"]
            if "content" in data:
                e["content"] = data["content"]
            memory.save_key_memory_entries(entries)
            return e

    raise HTTPException(status_code=404, detail=f"Entry {entry_id} not found.")


@router.delete("/instances/{instance_name}/key_memory/{entry_id}")
def delete_key_memory(instance_name: str, entry_id: str):
    """指定 ID の Key Memory エントリを削除する。"""
    components = deps.get_instance_components(instance_name)
    memory = components["memory"]
    entries = memory.get_key_memory_entries()

    new_entries = [e for e in entries if e["id"] != entry_id]
    if len(new_entries) == len(entries):
        raise HTTPException(status_code=404, detail=f"Entry {entry_id} not found.")

    memory.save_key_memory_entries(new_entries)
    return {"message": f"Entry {entry_id} deleted."}


# ==========================================
# 📝 Key Memory Proposals エンドポイント
# ==========================================


@router.get("/instances/{instance_name}/key_memory/proposals")
def get_key_memory_proposals(instance_name: str):
    """Key Memory 更新提案の一覧を返す。"""
    from butly_core.core.key_memory import load_proposals

    instance_dir = deps.INSTANCES_DIR / instance_name
    if not instance_dir.exists():
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")
    return {"proposals": load_proposals(instance_dir)}


@router.post("/instances/{instance_name}/key_memory/proposals/{proposal_idx}/approve")
def approve_key_memory_proposal(
    instance_name: str,
    proposal_idx: int,
    body: Dict[str, str] | None = Body(None),
):
    """提案を承認して Key Memory YAML に適用する。

    body (optional):
        content: str — 承認時に内容を上書きする場合に指定。
    """
    from butly_core.core.key_memory import (
        load_proposals,
        save_proposals,
        apply_proposal,
        YAML_FILENAME,
    )

    instance_dir = deps.INSTANCES_DIR / instance_name
    if not instance_dir.exists():
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")

    proposals = load_proposals(instance_dir)
    if proposal_idx < 0 or proposal_idx >= len(proposals):
        raise HTTPException(
            status_code=404, detail=f"Proposal index {proposal_idx} not found."
        )

    proposal = proposals[proposal_idx]
    if proposal.get("status") != "pending":
        raise HTTPException(status_code=400, detail="この提案は既に処理済みです。")

    # 承認時の内容上書き
    if body and "content" in body:
        if proposal["action"] == "update":
            proposal["new_content"] = body["content"]
        else:
            proposal["content"] = body["content"]

    yaml_path = instance_dir / YAML_FILENAME
    ok = apply_proposal(proposal, yaml_path)
    if not ok:
        raise HTTPException(status_code=500, detail="提案の適用に失敗しました。")

    proposal["status"] = "approved"
    save_proposals(proposals, instance_dir)
    return {"message": "提案を承認し適用しました。", "proposal": proposal}


@router.post("/instances/{instance_name}/key_memory/proposals/{proposal_idx}/reject")
def reject_key_memory_proposal(instance_name: str, proposal_idx: int):
    """提案を却下する。YAML は変更しない。"""
    from butly_core.core.key_memory import load_proposals, save_proposals

    instance_dir = deps.INSTANCES_DIR / instance_name
    if not instance_dir.exists():
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")

    proposals = load_proposals(instance_dir)
    if proposal_idx < 0 or proposal_idx >= len(proposals):
        raise HTTPException(
            status_code=404, detail=f"Proposal index {proposal_idx} not found."
        )

    proposal = proposals[proposal_idx]
    if proposal.get("status") != "pending":
        raise HTTPException(status_code=400, detail="この提案は既に処理済みです。")

    proposal["status"] = "rejected"
    save_proposals(proposals, instance_dir)
    return {"message": "提案を却下しました。", "proposal": proposal}
