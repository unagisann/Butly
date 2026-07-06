"""
version.py
──────────
frontend / backend の version handshake に使う定数。

BACKEND_VERSION は将来 packaging（PyInstaller sidecar）時に
単一の正としてここを更新する。
"""

BACKEND_VERSION = "0.1.0"

# `/api/v1` の contract version。breaking change 時のみ上げる。
API_VERSION = "v1"

API_V1_PREFIX = "/api/v1"


def is_api_v1_path(path: str) -> bool:
    """`/api/v1` そのもの、または `/api/v1/` 配下だけを true にする。

    `/api/v10/...` や `/api/v1evil` のような prefix 一致だけの path を
    v1 contract（error envelope / OpenAPI 公開対象）に含めないための判定。
    """
    return path == API_V1_PREFIX or path.startswith(API_V1_PREFIX + "/")
