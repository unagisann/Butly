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

# Gatekeeper / MemoryBlockBuilder のシングルトン
_gatekeeper = Gatekeeper(base_dir=DATA_DIR)
_mem_block_builder = MemoryBlockBuilder()

# --- Pydantic Models ---
class ChatRequest(BaseModel):
    message: str
    instance_name: str = "00_master"
    use_rag: bool = True
    use_google_search: bool = False
    images: List[str] = [] # list of base64 encoded strings

class ChatResponse(BaseModel):
    response: str
    keywords: List[str] = []
    references: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    tier: str = ""  # Gatekeeper の判定結果（reflex / mid / cortex）
    # Phase 2 追加
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
    cached_content = brain.prepare_cache(memory, ttl_hours=3, override_config=instance_config)
    
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

@app.post("/settings/api_key")
def set_api_key(request: ApiKeyRequest):
    """Gemini APIキーをDATA_DIR/.envに書き込み、即座にos.environに反映する"""
    key = request.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="APIキーが空です")

    # DATA_DIR/.env に書き込む
    env_path = DATA_DIR / ".env"
    env_path.write_text(f"GEMINI_API_KEY={key}\n", encoding="utf-8")

    # os.environ を即座に更新（サーバー再起動不要）
    os.environ["GEMINI_API_KEY"] = key

    print(f"[Server] API key updated and saved to {env_path}")
    return {"message": "APIキーを保存しました"}


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
    # Placeholder: expand as actual device integrations are added
    return {
        "fire_tv": {"status": "standby"},      # "active" | "standby" | "offline"
        "speaker": {"status": "connected"},    # "connected" | "offline"
        "mic": {"status": "off"},              # "on" | "off"
    }

@app.get("/discovery")
def get_discovery_items():
    # Placeholder: expand with real RSS parsing in Phase 6
    return {
        "items": []
    }

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
                # ToDo: brain.py に渡して応答を broadcast する
                text = data.get("payload", "")
                print(f"[WebSocket] chat_message: {text}")
                # モックエコー（brain.py 結合前の動作確認用）
                await ws_manager.broadcast({
                    "type": "ai_status", "payload": "thinking"
                })
                # 仮の応答エコー
                await ws_manager.broadcast({
                    "type": "chat_response",
                    "payload": f"[mock] received: {text}"
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        print("[WebSocket] Client disconnected")
    except Exception as e:
        ws_manager.disconnect(websocket)
        print(f"[WebSocket] Unexpected exception: {e}")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    """Interactions APIを使ってメッセージを送信し、応答を取得する。"""
    instance_name = request.instance_name
    components = get_instance_components(instance_name)
    
    memory = components["memory"]
    brain = components["brain"]
    chronos = components["chronos"]
    cached_content = components["cache"]

    # Time context
    last_ts = memory.get_last_interaction_time()
    sys_note = chronos.get_system_note(is_holiday=False, last_interaction_time=last_ts)
    full_prompt = f"{sys_note}\n\n{request.message}"

    # Instance config override
    instance_config = instance_manager.get_instance_config(instance_name)

    # -----------------------------------------------------------------
    # Gatekeeper V2: 構造化分類 + SessionState
    # -----------------------------------------------------------------
    # 直近履歴をロード（Gatekeeper の history_3_lines 用）
    history_for_gk, _ = memory.load_recent_sessions(limit=6)
    history_for_gemini = []
    for msg in history_for_gk:
        content = msg.get("parts", [""])[0]
        if isinstance(content, dict):
            content = content.get("text", "")
        history_for_gemini.append({"role": msg.get("role"), "parts": [content]})

    # ★ SessionState の読み込み
    instance_dir = INSTANCES_DIR / instance_name
    session_state = SessionState(instance_dir)

    # ★ Gatekeeper で構造化分類
    try:
        gk_result = _gatekeeper.classify(
            user_input=request.message,
            history_msgs=history_for_gemini,
            session_state=session_state.to_dict(),
        )
        tier = gk_result.get("tier", "mid")
    except Exception as e:
        print(f"[Chat] Gatekeeper エラー、フォールバック: {e}")
        gk_result = {"tier": "mid", "topic": "", "need": None, "search_targets": None, "state_delta": {}}
        tier = "mid"

    # ★ SessionState の更新
    state_delta = gk_result.get("state_delta", {})
    session_state.apply_delta(state_delta)
    session_state.increment_turn(tier)

    # tier に応じた記憶ブロックを構築（★ gatekeeper_output を追加）
    memory_blocks = _mem_block_builder.build(
        tier=tier,
        memory_manager=memory,
        brain=brain if tier == "cortex" else None,
        user_input=request.message,
        instance_name=instance_name,
        override_config=instance_config,
        gatekeeper_output=gk_result,
    )

    try:
        response_text = ""
        keywords = []
        refs = []
        sources = []

        if request.use_rag:
            # RAG モード: memory_blocks を start_chat へ渡す
            # cortex では RAG はすでに memory_blocks 内に含まれるため
            # generate_response_with_rag の内部 RAG は tier != cortex 時のみ意味を持つ
            response_text, keywords, refs, sources = await brain.generate_response_with_rag(
                user_input=full_prompt,
                memory_manager=memory,
                history=history_for_gemini,
                cached_content=cached_content,
                override_config=instance_config,
                use_google_search=request.use_google_search,
                images=request.images,
                memory_blocks=memory_blocks,
            )
        else:
            # Interactions API: ステートフルセッション方式
            previous_id = memory.load_interaction_id()
            print(f"[Chat] Instance: {instance_name}, tier: {tier}, prev_interaction_id: {previous_id}")

            response_text, new_interaction_id, sources = brain.chat_with_interactions(
                user_input=full_prompt,
                memory_manager=memory,
                override_config=instance_config,
                use_google_search=request.use_google_search,
                previous_interaction_id=previous_id,
                memory_blocks=memory_blocks,
            )
            memory.save_interaction_id(new_interaction_id)
            print(f"[Chat] Saved new interaction_id: {new_interaction_id}")

        # 会話をローカルに保存（将来の RAG / ナレッジ化用）
        memory.save_single_turn(request.message, response_text)
        memory.maintain_memory(brain)

        return ChatResponse(
            response=response_text,
            keywords=keywords if keywords else [],
            references=refs if refs else [],
            sources=sources if sources else [],
            tier=tier,
            # ★ Phase 2 追加
            need=gk_result.get("need"),
            search_targets=gk_result.get("search_targets"),
            session_state=session_state.to_dict(),
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
