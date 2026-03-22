from fastapi import FastAPI, HTTPException, Body, BackgroundTasks
from pydantic import BaseModel
from pathlib import Path
from typing import List, Optional, Dict, Any
import sys
import os
import argparse
import threading
import contextlib
import psutil
import platform
import time as time_module
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

# =====================================================================
# Phase 5: WebSocket Connection Manager
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

# Phase 5: brain.py 等の外部から AI ステータスを broadcast するヘルパー
async def notify_ai_status(status: str):
    """
    AIステータスを全クライアントに broadcast する。
    """
    await ws_manager.broadcast({"type": "ai_status", "payload": status})

# =====================================================================
# 起動引数のパース（--parent-pid, --port）
# =====================================================================
parser = argparse.ArgumentParser(description="Butly Backend Server", add_help=False)
parser.add_argument("--parent-pid", type=int, default=None, dest="parent_pid",
                    help="親プロセス(Flutter)のPID。このプロセスが死んだらサーバーも終了する。")
parser.add_argument("--port", type=int, default=48266,
                    help="サーバーのポート番号 (デフォルト: 48266)")
# Uvicornが渡す可能性がある余分な引数は無視する
args, _unknown = parser.parse_known_args()

# =====================================================================
# ゾンビプロセス防止: 親PIDの死亡を監視するバックグラウンドスレッド
# =====================================================================
def _watch_parent(parent_pid: int):
    """親プロセス(Flutter)が終了したらこのサーバーも強制終了する"""
    import time
    import ctypes
    print(f"[Server] Watching parent PID: {parent_pid}")
    while True:
        time.sleep(5)
        try:
            if sys.platform == "win32":
                # Windows: OpenProcess(SYNCHRONIZE, False, pid) が失敗したら死んでいる
                SYNCHRONIZE = 0x00100000
                handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
                if handle == 0:
                    print(f"[Server] Parent PID {parent_pid} is dead. Terminating server.")
                    os._exit(0)
                ctypes.windll.kernel32.CloseHandle(handle)
            else:
                # Linux/macOS: シグナル0を送ってプロセス生存確認
                os.kill(parent_pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            print(f"[Server] Parent PID {parent_pid} is dead. Terminating server.")
            os._exit(0)

if args.parent_pid:
    _watcher = threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True)
    _watcher.start()

# =====================================================================
# データディレクトリの解決
# PyInstallerでビルドされた場合はLOCALAPPDATA\Butly を使う
# それ以外（開発時）はserver/ディレクトリ直下を使う
# =====================================================================
if getattr(sys, 'frozen', False):
    # PyInstaller exe として実行された場合
    _appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = Path(_appdata) / "Butly"
else:
    # 開発時: スクリプトのある場所（= server/）
    DATA_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)
# butly_core/instances ディレクトリも事前に作成しておく
(DATA_DIR / "butly_core" / "instances").mkdir(parents=True, exist_ok=True)
print(f"[Server] Data directory: {DATA_DIR}")

# =====================================================================
# DATA_DIR内の.envからAPIキーを読み込む
# PyInstaller frozen時はLOCALAPPDATA\Butly\.envから
# 開発時はserver/.envから読み込む
# =====================================================================
def _load_env_from_data_dir():
    """DATA_DIR内の.envをos.environに読み込む"""
    env_path = DATA_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    os.environ.setdefault(key.strip(), value.strip())
        print(f"[Server] Loaded .env from {env_path}")
    else:
        print(f"[Server] No .env found at {env_path}. API key will need to be set via UI.")

_load_env_from_data_dir()

# Add current directory to sys.path to ensure modules can be imported
sys.path.append(str(Path(__file__).resolve().parent))

import json
from butly_core.config import SYSTEM_CONFIG
from butly_core.core.memory import ButlyMemory
from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.instance_manager import InstanceManager
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder, SessionState

# Fire TV モジュールの import（try で囲む: ADB 未インストール環境でも起動できるように）
try:
    from butly_core.core.fire_tv import get_status as tv_get_status, send_key, launch_app, KEYCODES, APPS
    FIRE_TV_AVAILABLE = True
except Exception as e:
    print(f"[FireTV] Module unavailable: {e}")
    FIRE_TV_AVAILABLE = False

# Gatekeeper / MemoryBlockBuilder のシングルトン
_gatekeeper = Gatekeeper(base_dir=DATA_DIR)
_mem_block_builder = MemoryBlockBuilder()

# --- Chat Service / DTO ---
from butly_core.chat.types import (
    ChatRequest as _InternalChatRequest,
    ChatResponse as _InternalChatResponse,
    normalize_ws_payload,
    validate_attachments,
    Attachment,
)
from butly_core.chat.service import ChatService

# --- Pydantic Models (REST API 用 — 旧互換) ---
class ChatRequest(BaseModel):
    """REST /chat 用リクエスト（旧互換）"""
    message: str
    instance_name: str = "00_master"
    use_rag: bool = True
    use_google_search: bool = False
    attachments: List[Dict[str, Any]] = []  # 新形式: Attachment 互換 dict リスト

class ChatResponse(BaseModel):
    response: str
    keywords: List[str] = []
    references: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    tier: str = ""  # Gatekeeper の判定結果（reflex / mid / cortex）
    need: Optional[str] = None
    search_targets: Optional[List[str]] = None
    session_state: Optional[Dict[str, Any]] = None

class HistoryRequest(BaseModel):
    instance_name: str = "00_master"
    limit: int = 10

class MessagePart(BaseModel):
    text: str

class Message(BaseModel):
    role: str
    parts: List[str] # Simplified for JSON response

# --- Global State ---
BASE_DIR = DATA_DIR
INSTANCES_DIR = DATA_DIR / "butly_core" / "instances"
instance_manager = InstanceManager(DATA_DIR)

# Store active instances: {instance_name: {'memory': m, 'brain': b, 'chronos': c, 'cache': cache}}
instance_store = {}

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------------------------------------------------------------
    # 初回起動時のセットアップ: 設定ファイルをDATA_DIRへコピー
    # (PyInstaller frozen時のみ。開発時はそのまま使う)
    # ---------------------------------------------------------------
    if getattr(sys, 'frozen', False):
        import shutil
        # PyInstallerがバンドルした元のファイル群はsys._MEIPASに展開される
        bundle_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        for config_file in ["user_config.json", "user_prompts.json", ".env"]:
            src = bundle_dir / config_file
            dst = DATA_DIR / config_file
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                print(f"[Server] Bootstrapped {config_file} to {dst}")

    # Startup: Ensure default instance exists
    if not INSTANCES_DIR.exists():
        INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
        (INSTANCES_DIR / "00_master").mkdir(exist_ok=True)
    print("Butly Server Started.")
    yield
    # Shutdown
    print("Butly Server Stopped.")


app = FastAPI(lifespan=lifespan, title="Butly API")

# --- CORS Configuration ---
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helper Functions ---
def get_instance_components(instance_name: str):
    if instance_name in instance_store:
        return instance_store[instance_name]
    
    # Initialize if not loaded
    if not (INSTANCES_DIR / instance_name).exists():
        raise HTTPException(status_code=404, detail=f"Instance '{instance_name}' not found.")
        
    print(f"[System] Initializing instance: {instance_name}")
    memory = ButlyMemory(BASE_DIR, instance_name=instance_name)
    brain = ButlyBrain(BASE_DIR)
    chronos = ButlyChronos()
    
    instance_config = instance_manager.get_instance_config(instance_name)
    from butly_core.llm.factory import ProviderFactory
    from butly_core.config import AI_CONFIG
    provider = ProviderFactory.create(AI_CONFIG["chat"]["model_name"])
    cached_content = provider.prepare_cache(memory, ttl_hours=3, override_config=instance_config) if hasattr(provider, "prepare_cache") else None
    
    components = {
        "memory": memory,
        "brain": brain,
        "chronos": chronos,
        "cache": cached_content,
        # Interactions API: セッション IDはファイルから一度読み出す
    }
    instance_store[instance_name] = components
    return components

# --- Endpoints ---

# --- New Models ---
class CreateInstanceRequest(BaseModel):
    name: str
    template: str = "{agent_name} is a helpful AI assistant."

class SettingsRequest(BaseModel):
    use_context_cache: Optional[bool] = None

# --- Helper for Settings Persistence ---
SETTINGS_FILE = BASE_DIR / "system_config.json"

def load_settings_from_file():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {}

def save_settings_to_file(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

# Apply settings on startup
current_settings = load_settings_from_file()
if "use_context_cache" in current_settings:
    SYSTEM_CONFIG["brain"]["use_context_cache"] = current_settings["use_context_cache"]

# --- Endpoints ---

@app.get("/instances")
def list_instances():
    """List all available AI instances."""
    if not INSTANCES_DIR.exists():
        return ["00_master"]
    instances = sorted([p.name for p in INSTANCES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])
    if not instances:
        return ["00_master"]
    return instances

@app.post("/instances")
def create_instance(request: CreateInstanceRequest):
    """Create a new AI instance."""
    success, message = instance_manager.create_instance(request.name, request.template)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message, "name": request.name}

class RenameInstanceRequest(BaseModel):
    new_name: str

@app.post("/instances/{instance_name}/rename")
def rename_instance(instance_name: str, request: RenameInstanceRequest):
    """Rename an existing AI instance."""
    success, result = instance_manager.rename_instance(instance_name, request.new_name)
    if not success:
        raise HTTPException(status_code=400, detail=result)
    
    # Clean up from memory store if loaded
    if instance_name in instance_store:
        del instance_store[instance_name]
        
    return {"message": "Instance renamed", "new_instance_name": result}

@app.delete("/instances/{instance_name}")
def delete_instance(instance_name: str):
    """Delete an existing AI instance."""
    if instance_name == "00_master":
        raise HTTPException(status_code=400, detail="Cannot delete 00_master.")
        
    success, result = instance_manager.delete_instance(instance_name)
    if not success:
        raise HTTPException(status_code=400, detail=result)
    
    # Clean up from memory store if loaded
    if instance_name in instance_store:
        del instance_store[instance_name]
        
    return {"message": "Instance deleted", "instance_name": instance_name}

@app.get("/settings")
def get_settings():
    """Get current system settings."""
    return {
        "use_context_cache": SYSTEM_CONFIG["brain"].get("use_context_cache", True)
    }

@app.post("/settings")
def update_settings(request: SettingsRequest):
    """Update system settings."""
    # Update memory
    if request.use_context_cache is not None:
        SYSTEM_CONFIG["brain"]["use_context_cache"] = request.use_context_cache
    
    # Persist
    current_settings = load_settings_from_file()
    if request.use_context_cache is not None:
        current_settings["use_context_cache"] = request.use_context_cache
    save_settings_to_file(current_settings)
    
    return {"message": "Settings updated", "settings": get_settings()}

class ApiKeyRequest(BaseModel):
    api_key: str
    key_type: str = "gemini"  # "gemini" | "openai"

@app.post("/settings/api_key")
def set_api_key(request: ApiKeyRequest):
    """APIキーをDATA_DIR/.envに書き込み、即座にos.environに反映する"""
    key = request.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="APIキーが空です")

    env_path = DATA_DIR / ".env"

    # 既存の .env を読み込み、該当キーのみ更新
    env_vars = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                env_vars[k.strip()] = v.strip()

    if request.key_type == "openai":
        env_vars["OPENAI_API_KEY"] = key
        os.environ["OPENAI_API_KEY"] = key
    else:
        env_vars["GOOGLE_API_KEY"] = key
        os.environ["GOOGLE_API_KEY"] = key

    # .env を書き直す
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[Server] {request.key_type} API key updated and saved to {env_path}")
    return {"message": f"{request.key_type} APIキーを保存しました"}

@app.get("/settings/api_key_status")
def get_api_key_status():
    """各プロバイダーのAPIキー設定状況を返す"""
    return {
        "gemini": bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }

@app.post("/settings/ollama_test")
def test_ollama_connection(url: str = Body(..., embed=True)):
    """Ollama の接続テストを行う"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return {"status": "ok", "models": models}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/settings/reindex_embeddings")
async def reindex_embeddings(
    instance_name: str = Body("__all__", embed=True),
    background_tasks: BackgroundTasks = None,
):
    """Embedding再生成をバックグラウンドで実行"""
    from migrate_embeddings import migrate_instance as _migrate_inst

    def _run_reindex(target: str):
        if target == "__all__":
            instances_dir = BASE_DIR / "butly_core" / "instances"
            for d in sorted(instances_dir.iterdir()):
                if d.is_dir():
                    db_p = d / SYSTEM_CONFIG["paths"]["db_name"]
                    if db_p.exists():
                        _migrate_inst(d.name)
        else:
            _migrate_inst(target)

    background_tasks.add_task(run_in_threadpool, _run_reindex, instance_name)
    return {"message": "Embedding再生成を開始しました", "target": instance_name}


# --- Phase 4 & 5 UI Dashboard Endpoints ---

@app.get("/status")
def get_system_status():
    cpu_temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key in ['cpu_thermal', 'coretemp', 'k10temp']:
                if key in temps:
                    cpu_temp = round(temps[key][0].current, 1)
                    break
    except Exception:
        pass

    net_connected = False
    try:
        import socket
        socket.create_connection(("8.8.8.8", 53), timeout=1)
        net_connected = True
    except Exception:
        pass

    return {
        "cpu_temp": cpu_temp,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": psutil.virtual_memory().percent,
        "net_connected": net_connected,
        "platform": platform.system(),
        "uptime_seconds": int(time_module.time() - psutil.boot_time()),  # elapsed seconds since boot
    }

@app.get("/devices")
def get_device_status():
    """接続デバイスのリアルタイム状態を返す"""
    fire_tv = {"status": "offline", "current_app": None}
    if FIRE_TV_AVAILABLE:
        try:
            fire_tv = tv_get_status()
        except Exception as e:
            print(f"[FireTV] Status error: {e}")

    return {
        "fire_tv": fire_tv,
        "speaker": {"status": "connected"},  # 将来: スピーカー連携
        "mic": {"status": "off"},
    }

# Fire TV コントロールエンドポイント（新規追加）
class TvKeyRequest(BaseModel):
    key: str  # "home" | "back" | "play_pause" | etc.

class TvLaunchRequest(BaseModel):
    app: str  # "youtube" | "twitch" | etc.

@app.post("/tv/key")
def tv_send_key(request: TvKeyRequest):
    """Fire TV にキー入力を送信する"""
    if not FIRE_TV_AVAILABLE:
        raise HTTPException(status_code=503, detail="Fire TV module unavailable")
    keycode = KEYCODES.get(request.key)
    if not keycode:
        raise HTTPException(status_code=400, detail=f"Unknown key: {request.key}. Valid: {list(KEYCODES.keys())}")
    ok = send_key(keycode)
    return {"success": ok, "key": request.key, "keycode": keycode}

@app.post("/tv/launch")
def tv_launch_app(request: TvLaunchRequest):
    """Fire TV でアプリを起動する"""
    if not FIRE_TV_AVAILABLE:
        raise HTTPException(status_code=503, detail="Fire TV module unavailable")
    package = APPS.get(request.app)
    if not package:
        raise HTTPException(status_code=400, detail=f"Unknown app: {request.app}. Valid: {list(APPS.keys())}")
    ok = launch_app(package)
    return {"success": ok, "app": request.app, "package": package}

DISCOVERY_CACHE_PATH = BASE_DIR / "discovery_cache.json"

@app.get("/discovery")
def get_discovery_items():
    """
    discovery_agent.py が生成したキャッシュを返す。
    キャッシュがない場合は空データを返す（ダッシュボードが落ちないように）。
    """
    if not DISCOVERY_CACHE_PATH.exists():
        return {
            "updated_at": None,
            "sections": [],
            "jarvis_comment": "",
        }
    try:
        data = json.loads(DISCOVERY_CACHE_PATH.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"[Discovery] Cache read error: {e}")
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}

NEWS_CACHE_PATH = BASE_DIR / "news_cache.json"

@app.get("/news")
def get_news_items():
    """
    news_agent.py が生成したキャッシュを返す。
    キャッシュがない場合は空データを返す。
    """
    if not NEWS_CACHE_PATH.exists():
        return {
            "updated_at": None,
            "sections": [],
            "jarvis_comment": "",
        }
    try:
        data = json.loads(NEWS_CACHE_PATH.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"[News] Cache read error: {e}")
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}       

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Phase 5: Real-time UI connection.
    ダッシュボードのアニメーションステータスや即時反映のモック・将来のbrain連携の受け口。
    """
    await ws_manager.connect(websocket)
    try:
        while True:
            # ToDo Phase 5 後半: 音声バイナリは receive_bytes() に切り替え
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # JSON パース失敗は切断せず無視して次のメッセージを待つ
                continue

            msg_type = data.get("type")

            if msg_type == "mic_control":
                status = data.get("payload")
                print(f"[WebSocket] mic_control: {status}")
                await ws_manager.broadcast({
                    "type": "ai_status",
                    "payload": "listening" if status == "on" else "idle"
                })

            elif msg_type == "chat_message":
                raw_payload = data.get("payload", "")

                # payload を共通 DTO に正規化
                try:
                    chat_request = normalize_ws_payload(raw_payload)
                except Exception as e:
                    print(f"[WebSocket] payload 正規化エラー: {e}")
                    continue

                if not chat_request.text and not chat_request.attachments:
                    continue

                att_info = f", attachments={len(chat_request.attachments)}" if chat_request.attachments else ""
                print(f"[WebSocket] chat_message: {chat_request.text[:50]}{att_info}")

                # thinking 状態を通知
                await ws_manager.broadcast({"type": "ai_status", "payload": "thinking"})

                try:
                    # ChatService に委譲
                    result = await ChatService.execute(
                        request=chat_request,
                        get_instance_components=get_instance_components,
                        instance_manager=instance_manager,
                        instances_dir=INSTANCES_DIR,
                        gatekeeper=_gatekeeper,
                        mem_block_builder=_mem_block_builder,
                        ws_manager=ws_manager,
                    )

                    # speaking 状態 → 応答送信
                    await ws_manager.broadcast({"type": "ai_status", "payload": "speaking"})
                    await ws_manager.broadcast({"type": "chat_response", "payload": result.text})

                    # idle に戻す
                    await ws_manager.broadcast({"type": "ai_status", "payload": "idle"})

                except Exception as e:
                    print(f"[WebSocket] chat error: {e}")
                    await ws_manager.broadcast({"type": "ai_status", "payload": "idle"})
                    await ws_manager.broadcast({
                        "type": "chat_response",
                        "payload": f"[エラー] 応答の生成に失敗しました: {str(e)[:100]}"
                    })
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        print("[WebSocket] Client disconnected")
    except Exception as e:
        ws_manager.disconnect(websocket)
        print(f"[WebSocket] Unexpected exception: {e}")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    """チャットメッセージを送信し、応答を取得する（ChatService に委譲）。"""
    # REST リクエストを内部 DTO に変換
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
            get_instance_components=get_instance_components,
            instance_manager=instance_manager,
            instances_dir=INSTANCES_DIR,
            gatekeeper=_gatekeeper,
            mem_block_builder=_mem_block_builder,
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

@app.post("/instances/{instance_name}/reload")
def reload_instance(instance_name: str):
    """セッションをリセットする（last_interaction_id.txtをクリア + メモリキャッシュ削除 + SessionStateクリア）"""
    # メモリキャッシュを削除（次回リクエスト時に再初期化）
    if instance_name in instance_store:
        del instance_store[instance_name]
    
    # インスタンスのlast_interaction_id.txtをクリア
    instance_dir = INSTANCES_DIR / instance_name
    if instance_dir.exists():
        id_file = instance_dir / "last_interaction_id.txt"
        if id_file.exists():
            id_file.unlink()
            
    # ★ SessionState もリセット
    try:
        ss = SessionState(instance_dir)
        ss.reset()
    except Exception:
        pass
    
    return {"message": f"Session for '{instance_name}' has been reset."}

@app.get("/instances/{instance_name}/config")
def get_instance_config(instance_name: str):
    """Get instance-specific configuration."""
    return instance_manager.get_instance_config(instance_name)

@app.post("/instances/{instance_name}/config")
def update_instance_config(instance_name: str, config: Dict[str, Any] = Body(...)):
    """Update instance-specific configuration."""
    success, message = instance_manager.update_instance_config(instance_name, config)
    if not success:
         raise HTTPException(status_code=500, detail=message)
    return {"message": message, "config": instance_manager.get_instance_config(instance_name)}

@app.get("/instances/{instance_name}/prompts")
def get_instance_prompts(instance_name: str):
    """インスタンスごとのプロンプトテキスト (system_instruction, key_memory) を取得"""
    result = instance_manager.get_instance_prompts(instance_name)
    if result is None:
        raise HTTPException(status_code=404, detail="インスタンスが存在しません。")
    return result

@app.post("/instances/{instance_name}/prompts")
def update_instance_prompts(instance_name: str, data: Dict[str, str] = Body(...)):
    """インスタンスごとのプロンプトテキストを保存"""
    success, message = instance_manager.update_instance_prompts(instance_name, data)
    if not success:
        raise HTTPException(status_code=500, detail=message)
    return {"message": message}

@app.get("/history/{instance_name}")
def get_history(instance_name: str, limit: int = 10):
    """Get recent chat history."""
    components = get_instance_components(instance_name)
    memory = components["memory"]
    
    chat_history = memory.load_recent_sessions(limit=limit) # Returns (messages, last_ts)
    history_msgs = chat_history[0] if isinstance(chat_history, tuple) else chat_history
    
    formatted_history = []
    for msg in history_msgs:
        content = msg.get("parts", [""])[0]
        if isinstance(content, dict): 
            content = content.get("text", "")
        formatted_history.append({"role": msg.get("role"), "parts": [content]})
        
    return formatted_history

# --- Config & Prompts API ---
from butly_core.config import AI_CONFIG, SYSTEM_CONFIG, USER_CONFIG_PATH, _recursive_update
import butly_core.prompts as prompts_module
from butly_core.prompts import USER_PROMPTS_PATH

EDITABLE_PROMPTS = [
    "HOUSEKEEPER_SUMMARIZE_PROMPT",
    "BRAIN_EXTRACT_KEYWORDS_PROMPT",
    "BRAIN_SUMMARIZE_CONVERSATION_PROMPT",
    "GATEKEEPER_CLASSIFY_PROMPT",
    "WEB_UI_DEFAULT_TEMPLATE"
]

@app.get("/config")
def get_config():
    """Get current effective configuration."""
    return {"AI_CONFIG": AI_CONFIG, "SYSTEM_CONFIG": SYSTEM_CONFIG}

@app.post("/config")
def update_config(config_data: Dict[str, Any] = Body(...)):
    """Update configuration and save to user_config.json."""
    try:
        # Save to file
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
        
        # Update in memory
        if "AI_CONFIG" in config_data:
            _recursive_update(AI_CONFIG, config_data["AI_CONFIG"])
        if "SYSTEM_CONFIG" in config_data:
            _recursive_update(SYSTEM_CONFIG, config_data["SYSTEM_CONFIG"])
            
        return {"message": "Config updated", "config": get_config()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")

@app.get("/prompts")
def get_prompts():
    """Get editable prompts."""
    return {key: getattr(prompts_module, key, "") for key in EDITABLE_PROMPTS}

@app.post("/prompts")
def update_prompts(prompts_data: Dict[str, str] = Body(...)):
    """Update prompts and save to user_prompts.json."""
    try:
        # Filter only allowed prompts
        safe_data = {k: v for k, v in prompts_data.items() if k in EDITABLE_PROMPTS}
        
        # Save to file
        with open(USER_PROMPTS_PATH, "w", encoding="utf-8") as f:
            json.dump(safe_data, f, indent=4, ensure_ascii=False)
            
        # Update in memory
        for key, value in safe_data.items():
            if hasattr(prompts_module, key):
                setattr(prompts_module, key, value)
                
        return {"message": "Prompts updated", "prompts": get_prompts()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompts: {e}")

# --- Housekeeper API ---
from housekeeper import ButlyHousekeeper, housekeeper_store
from fastapi.concurrency import run_in_threadpool

housekeeper_instance = ButlyHousekeeper()

@app.get("/housekeeper/estimate/{instance_name}")
def estimate_housekeeper(instance_name: str):
    """Get estimated workload for Housekeeper."""
    try:
        result = housekeeper_instance.estimate_workload(instance_name)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class HousekeeperRunRequest(BaseModel):
    instance_name: str

@app.post("/housekeeper/run")
async def run_housekeeper(request: HousekeeperRunRequest, background_tasks: BackgroundTasks):
    """Run Housekeeper in background."""
    instance_name = request.instance_name
    
    # Check if already running
    current_status = housekeeper_store.get(instance_name, {})
    if current_status.get("state") == "running":
        return {"message": "Housekeeper is already running", "status": current_status}
    
    # Initialize status
    housekeeper_instance.update_status(instance_name, "running", 0.0, "Starting...")
    
    # Run in thread pool to avoid blocking event loop
    background_tasks.add_task(run_in_threadpool, housekeeper_instance.run_with_progress, instance_name)
    
    return {"message": "Housekeeper started", "status": housekeeper_store[instance_name]}

@app.get("/housekeeper/status/{instance_name}")
def get_housekeeper_status(instance_name: str):
    """Get current Housekeeper status."""
    return housekeeper_store.get(instance_name, {"state": "idle", "progress": 0.0, "message": ""})

# --- Database Browser API ---
from butly_core.core.database import ButlyDatabase

@app.get("/database/cards/{instance_name}")
def get_database_cards(instance_name: str, limit: int = 50, offset: int = 0, category: Optional[str] = None, search: Optional[str] = None):
    """Get knowledge cards from the instance's database."""
    db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    cards = db.get_cards(limit=limit, offset=offset, category=category, search=search)
    return cards

@app.get("/database/cards/{instance_name}/{card_id}")
def get_database_card(instance_name: str, card_id: str):
    """Get details of a specific knowledge card."""
    db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    card = db.get_card(card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card

class UpdateCardRequest(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[str] = None
    summary: Optional[str] = None
    episode: Optional[str] = None
    ai_importance: Optional[int] = None
    humanity_importance: Optional[int] = None

class CardPinRequest(BaseModel):
    is_pinned: bool

@app.post("/database/cards/{instance_name}/{card_id}/pin")
def pin_database_card(instance_name: str, card_id: str, request: CardPinRequest):
    """Pin or unpin a knowledge card, and if pinning, append it to Key_Memory.txt"""
    db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    
    success = db.toggle_pin(card_id, request.is_pinned)
    if not success:
        raise HTTPException(status_code=404, detail="Card not found or failed to update pin state")
        
    if request.is_pinned:
        card = db.get_card(card_id)
        if card:
            # Key_Memory.txtへ追記
            km_path = INSTANCES_DIR / instance_name / "Key_Memory.txt"
            additional_text = f"\n\n[Pinned Memory: {card['title']}]\n{card['summary']}"
            if km_path.exists():
                with open(km_path, "a", encoding="utf-8") as f:
                    f.write(additional_text)
            else:
                km_path.write_text(additional_text.strip(), encoding="utf-8")
                
    return {"message": "Card pin state updated successfully", "is_pinned": request.is_pinned}


@app.put("/database/cards/{instance_name}/{card_id}")
def update_database_card(instance_name: str, card_id: str, request: UpdateCardRequest):
    """Update a knowledge card."""
    db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    update_data = request.dict(exclude_none=True)
    if not update_data:
        return {"message": "No fields to update"}
        
    success = db.update_card(card_id, update_data)
    if not success:
        raise HTTPException(status_code=404, detail="Card not found or failed to update")
    return {"message": "Card updated successfully"}

@app.delete("/database/cards/{instance_name}/{card_id}")
def delete_database_card(instance_name: str, card_id: str):
    """Delete a knowledge card."""
    db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
    db = ButlyDatabase(db_path=str(db_path))
    success = db.delete_card(card_id)
    if not success:
        raise HTTPException(status_code=404, detail="Card not found or failed to delete")
    return {"message": "Card deleted successfully"}
