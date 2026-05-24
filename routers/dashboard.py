"""
routers/dashboard.py
────────────────────
システムステータス / Discovery / News エンドポイント。
"""

import json
import platform
import time as time_module

import psutil
from fastapi import APIRouter

import dependencies as deps

router = APIRouter()


@router.get("/status")
def get_system_status():
    cpu_temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key in ["cpu_thermal", "coretemp", "k10temp"]:
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
        "uptime_seconds": int(time_module.time() - psutil.boot_time()),
    }


@router.get("/discovery")
def get_discovery_items():
    """discovery_agent.py が生成したキャッシュを返す。"""
    cache_path = deps.BASE_DIR / "discovery_cache.json"
    if not cache_path.exists():
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"[Discovery] Cache read error: {e}")
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}


@router.get("/news")
def get_news_items():
    """news_agent.py が生成したキャッシュを返す。"""
    cache_path = deps.BASE_DIR / "news_cache.json"
    if not cache_path.exists():
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        print(f"[News] Cache read error: {e}")
        return {"updated_at": None, "sections": [], "jarvis_comment": ""}
