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

class RenameInstanceRequest(BaseModel):
    new_name: str


@router.get("/instances")
def list_instances():
    """List all available AI instances."""
    if not deps.INSTANCES_DIR.exists():
        return []
    instances = sorted([
        p.name for p in deps.INSTANCES_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ])
    return instances


@router.post("/instances")
def create_instance(request: CreateInstanceRequest):
    """Create a new AI instance."""
    success, message = deps.instance_manager.create_instance(
        request.name,
        request.template,
        key_memory=request.key_memory,
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message, "name": request.name}


@router.post("/instances/{instance_name}/rename")
def rename_instance(instance_name: str, request: RenameInstanceRequest):
    """Rename an existing AI instance."""
    success, result = deps.instance_manager.rename_instance(instance_name, request.new_name)
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
    success, message = deps.instance_manager.update_instance_config(instance_name, config)
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return {"message": message, "config": deps.instance_manager.get_instance_config(instance_name)}


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
    success, message = deps.instance_manager.update_instance_prompts(instance_name, data)
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
