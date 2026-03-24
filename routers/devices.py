"""
routers/devices.py
──────────────────
Fire TV + 将来のデバイス制御エンドポイント。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import dependencies as deps

router = APIRouter()


@router.get("/devices")
def get_device_status():
    """接続デバイスのリアルタイム状態を返す。"""
    fire_tv = {"status": "offline", "current_app": None}
    if deps.FIRE_TV_AVAILABLE:
        try:
            fire_tv = deps.tv_get_status()
        except Exception as e:
            print(f"[FireTV] Status error: {e}")

    return {
        "fire_tv": fire_tv,
        "speaker": {"status": "connected"},
        "mic": {"status": "off"},
    }


class TvKeyRequest(BaseModel):
    key: str

class TvLaunchRequest(BaseModel):
    app: str


@router.post("/tv/key")
def tv_send_key(request: TvKeyRequest):
    """Fire TV にキー入力を送信する。"""
    if not deps.FIRE_TV_AVAILABLE:
        raise HTTPException(status_code=503, detail="Fire TV module unavailable")
    keycode = deps.KEYCODES.get(request.key)
    if not keycode:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown key: {request.key}. Valid: {list(deps.KEYCODES.keys())}",
        )
    ok = deps.send_key(keycode)
    return {"success": ok, "key": request.key, "keycode": keycode}


@router.post("/tv/launch")
def tv_launch_app(request: TvLaunchRequest):
    """Fire TV でアプリを起動する。"""
    if not deps.FIRE_TV_AVAILABLE:
        raise HTTPException(status_code=503, detail="Fire TV module unavailable")
    package = deps.APPS.get(request.app)
    if not package:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown app: {request.app}. Valid: {list(deps.APPS.keys())}",
        )
    ok = deps.launch_app(package)
    return {"success": ok, "app": request.app, "package": package}
