"""
fire_tv.py
----------
ADB over TCP で Fire TV を制御するモジュール。
main.py の /devices, /tv/* エンドポイントから呼び出す。
"""
import os
import subprocess
import time
from typing import Optional

# ========================================================
# 設定（main.py の環境変数から読む想定。直接編集でも可）
# ========================================================
FIRE_TV_IP = os.getenv("FIRE_TV_IP", "192.168.10.105")
FIRE_TV_PORT = int(os.getenv("FIRE_TV_PORT", "5555"))
ADB_TIMEOUT = 3               # コマンドタイムアウト（秒）

# 接続状態キャッシュ（30秒有効）
_connection_cache: dict = {"status": "unknown", "checked_at": 0}
CACHE_TTL = 30


def _run_adb(args: list[str]) -> tuple[bool, str]:
    """ADB コマンドを実行して (成功フラグ, 出力テキスト) を返す"""
    try:
        result = subprocess.run(
            ["adb"] + args,
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT,
        )
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "adb not found"
    except Exception as e:
        return False, str(e)


def connect() -> bool:
    """Fire TV に ADB 接続する"""
    ok, out = _run_adb(["connect", f"{FIRE_TV_IP}:{FIRE_TV_PORT}"])
    return ok and "connected" in out.lower()


def get_status() -> dict:
    """
    接続状態と現在の再生状態を返す。
    キャッシュ TTL 内なら再チェックしない。
    
    Returns:
        {
            "status": "active" | "standby" | "offline",
            "current_app": str | None,   # 現在フォアグラウンドのパッケージ名
        }
    """
    now = time.time()
    if now - _connection_cache["checked_at"] < CACHE_TTL:
        return _connection_cache.copy()

    # 接続確認
    ok, out = _run_adb(["devices"])
    is_connected = f"{FIRE_TV_IP}:{FIRE_TV_PORT}" in out and "offline" not in out

    if not is_connected:
        # 再接続試行
        is_connected = connect()

    if not is_connected:
        result = {"status": "offline", "current_app": None}
    else:
        # フォアグラウンドアプリを取得
        ok2, app_out = _run_adb([
            "-s", f"{FIRE_TV_IP}:{FIRE_TV_PORT}",
            "shell", "dumpsys", "window", "windows",
        ])
        current_app = None
        if ok2 and "mCurrentFocus" in app_out:
            # "mCurrentFocus=Window{... com.amazon.firetv.launcher/...}"
            import re
            m = re.search(r"mCurrentFocus=Window\{[^}]+ ([^/]+)/", app_out)
            if m:
                current_app = m.group(1)
        elif not ok2:
            # mCurrentFocus抽出用のgrepはPython処理に置き換えた（subprocess依存軽減のため）
            pass

        # ランチャーにいれば standby 扱い
        is_standby = current_app in (
            "com.amazon.firetv.launcher",
            "com.amazon.tv.launcher",
            None,
        )
        result = {
            "status": "standby" if is_standby else "active",
            "current_app": current_app,
        }

    _connection_cache.update({**result, "checked_at": now})
    return result


# ========================================================
# コントロールコマンド
# ========================================================

def send_key(keycode: str) -> bool:
    """キー入力を送信する（KEYCODE_HOME, KEYCODE_BACK, etc.）"""
    ok, _ = _run_adb([
        "-s", f"{FIRE_TV_IP}:{FIRE_TV_PORT}",
        "shell", "input", "keyevent", keycode
    ])
    return ok


def launch_app(package: str) -> bool:
    """指定パッケージ名のアプリを起動する"""
    ok, _ = _run_adb([
        "-s", f"{FIRE_TV_IP}:{FIRE_TV_PORT}",
        "shell", "monkey", "-p", package,
        "-c", "android.intent.category.LAUNCHER", "1"
    ])
    return ok


# ========================================================
# よく使うショートカット
# ========================================================

KEYCODES = {
    "home":        "KEYCODE_HOME",
    "back":        "KEYCODE_BACK",
    "play_pause":  "KEYCODE_MEDIA_PLAY_PAUSE",
    "next":        "KEYCODE_MEDIA_NEXT",
    "prev":        "KEYCODE_MEDIA_PREVIOUS",
    "volume_up":   "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute":        "KEYCODE_VOLUME_MUTE",
    "select":      "KEYCODE_DPAD_CENTER",
    "up":          "KEYCODE_DPAD_UP",
    "down":        "KEYCODE_DPAD_DOWN",
    "left":        "KEYCODE_DPAD_LEFT",
    "right":       "KEYCODE_DPAD_RIGHT",
}

APPS = {
    "youtube":  "com.amazon.firetv.youtube",
    "twitch":   "tv.twitch.android.viewer",
    "netflix":  "com.netflix.ninja",
    "prime":    "com.amazon.avod.thirdpartyclient",
    "launcher": "com.amazon.firetv.launcher",
}
