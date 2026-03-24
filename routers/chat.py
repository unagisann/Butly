"""
routers/chat.py
───────────────
チャット REST + WebSocket エンドポイント。
"""
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.websockets import WebSocketState

from butly_core.chat.types import (
    ChatRequest as _InternalChatRequest,
    normalize_ws_payload,
    Attachment,
)
from butly_core.chat.service import ChatService

import dependencies as deps

router = APIRouter()


# =====================================================================
# WebSocket Connection Manager
# =====================================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        if getattr(websocket, "application_state", None) != WebSocketState.CONNECTED:
            await websocket.accept()
        if websocket not in self.active_connections:
            self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"[WebSocket] Error broadcasting to client: {e}")


ws_manager = ConnectionManager()


async def notify_ai_status(status: str):
    """AIステータスを全クライアントに broadcast する。"""
    await ws_manager.broadcast({"type": "ai_status", "payload": status})


# =====================================================================
# Pydantic Models (REST /chat 用)
# =====================================================================
class ChatRequest(BaseModel):
    """REST /chat 用リクエスト（旧互換）"""
    message: str
    instance_name: str = "00_master"
    use_rag: bool = True
    use_google_search: bool = False
    attachments: List[Dict[str, Any]] = []

class ChatResponse(BaseModel):
    response: str
    keywords: List[str] = []
    references: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    tier: str = ""
    need: Optional[str] = None
    search_targets: Optional[List[str]] = None
    session_state: Optional[Dict[str, Any]] = None


# =====================================================================
# REST Endpoint
# =====================================================================
@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    """チャットメッセージを送信し、応答を取得する（ChatService に委譲）。"""
    attachments = []
    for att_data in request.attachments:
        try:
            attachments.append(Attachment(**att_data))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"添付データの形式が不正です: {e}")

    internal_request = _InternalChatRequest(
        text=request.message,
        attachments=attachments,
        instance_name=request.instance_name,
        use_rag=request.use_rag,
        use_google_search=request.use_google_search,
    )

    try:
        result = await ChatService.execute(
            request=internal_request,
            get_instance_components=deps.get_instance_components,
            instance_manager=deps.instance_manager,
            instances_dir=deps.INSTANCES_DIR,
            gatekeeper=deps.gatekeeper,
            mem_block_builder=deps.mem_block_builder,
        )

        return ChatResponse(
            response=result.text,
            keywords=result.keywords if result.keywords else [],
            references=result.refs if result.refs else [],
            sources=result.sources if result.sources else [],
            tier=result.tier,
            need=result.need,
            search_targets=result.search_targets,
            session_state=result.session_state,
        )

    except Exception as e:
        print(f"Error during chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# WebSocket Endpoint
# =====================================================================
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time UI connection."""
    await ws_manager.connect(websocket)
    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                continue

            msg_type = data.get("type")

            if msg_type == "mic_control":
                status = data.get("payload")
                print(f"[WebSocket] mic_control: {status}")
                await ws_manager.broadcast({
                    "type": "ai_status",
                    "payload": "listening" if status == "on" else "idle",
                })

            elif msg_type == "chat_message":
                raw_payload = data.get("payload", "")

                try:
                    chat_request = normalize_ws_payload(raw_payload)
                except Exception as e:
                    print(f"[WebSocket] payload 正規化エラー: {e}")
                    continue

                if not chat_request.text and not chat_request.attachments:
                    continue

                att_info = f", attachments={len(chat_request.attachments)}" if chat_request.attachments else ""
                print(f"[WebSocket] chat_message: {chat_request.text[:50]}{att_info}")

                await ws_manager.broadcast({"type": "ai_status", "payload": "thinking"})

                try:
                    result = await ChatService.execute(
                        request=chat_request,
                        get_instance_components=deps.get_instance_components,
                        instance_manager=deps.instance_manager,
                        instances_dir=deps.INSTANCES_DIR,
                        gatekeeper=deps.gatekeeper,
                        mem_block_builder=deps.mem_block_builder,
                        ws_manager=ws_manager,
                    )

                    await ws_manager.broadcast({"type": "ai_status", "payload": "speaking"})
                    await ws_manager.broadcast({"type": "chat_response", "payload": result.text})
                    await ws_manager.broadcast({"type": "ai_status", "payload": "idle"})

                except Exception as e:
                    print(f"[WebSocket] chat error: {e}")
                    await ws_manager.broadcast({"type": "ai_status", "payload": "idle"})
                    await ws_manager.broadcast({
                        "type": "chat_response",
                        "payload": f"[エラー] 応答の生成に失敗しました: {str(e)[:100]}",
                    })
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        print("[WebSocket] Client disconnected")
    except Exception as e:
        ws_manager.disconnect(websocket)
        print(f"[WebSocket] Unexpected exception: {e}")
