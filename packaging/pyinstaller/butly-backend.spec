# -*- mode: python ; coding: utf-8 -*-
"""
butly-backend.spec
──────────────────
FastAPI sidecar（butly_api/server.py）の PyInstaller spec。

- 既定は one-folder（起動時間 / AV 誤検知の観点。移行計画 §6.5）。
  環境変数 BUTLY_SIDECAR_ONEFILE=1 で one-file に切り替えられる
  （Tauri 同梱で one-folder が不安定だった場合の代替。切替時は理由と
  起動時間を docs/reference/desktop_sidecar.ja.md に記録する）。
- `.env` / 実 user_config.json / user_prompts.json / instances は
  **絶対に bundle へ含めない**（雛形 *.example のみ同梱する）。
  scripts/build_backend_sidecar.py が build 後に混入チェックを行う。
"""

import os
from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 (SPECPATH is provided by PyInstaller)
ONEFILE = os.environ.get("BUTLY_SIDECAR_ONEFILE") == "1"

prompts_dir = REPO_ROOT / "butly_core" / "prompts"

datas = [
    (str(prompts_dir / "control"), "butly_core/prompts/control"),
    (str(prompts_dir / "locales"), "butly_core/prompts/locales"),
    (str(prompts_dir / "prompt_registry.yaml"), "butly_core/prompts"),
    # 雛形のみ同梱。実 config は backend が初回起動時に data dir へ生成する
    (str(REPO_ROOT / "user_config.json.example"), "."),
    (str(REPO_ROOT / "persons.json.example"), "."),
]

hiddenimports = [
    # tiktoken は plugin 機構で dynamic import されるため明示する
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
]

try:
    from PyInstaller.utils.hooks import copy_metadata

    # importlib.metadata でバージョン参照する SDK の dist-info を同梱
    for pkg in ("google-genai", "openai", "tiktoken"):
        datas += copy_metadata(pkg)
except Exception as e:  # metadata が無くても build 自体は続行する
    print(f"[spec] copy_metadata skipped: {e}")

a = Analysis(
    [str(Path(SPECPATH) / "sidecar_entry.py")],  # noqa: F821
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # sidecar は /api/v1 のみ。Streamlit UI 系は含めない
        "streamlit",
        "altair",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="butly-backend",
        debug=False,
        strip=False,
        upx=False,
        console=True,  # stdout の listening JSON を Tauri が読む
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="butly-backend",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="butly-backend",
    )
