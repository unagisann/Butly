"""
build_backend_sidecar.py
------------------------
PyInstaller で FastAPI sidecar を build し、Tauri が要求する配置へコピーする
再現可能な build スクリプト。

- one-folder（既定）:
    packaging/pyinstaller/dist/butly-backend/
      butly-backend(.exe)  → frontend/src-tauri/binaries/butly-backend-<triple>(.exe)
      _internal/           → frontend/src-tauri/resources/backend/_internal/
  Tauri externalBin は exe を install root へ、resources は `_internal/` を
  同じ install root へ置くため、PyInstaller one-folder の exe と _internal の
  隣接関係が保たれる。
- one-file（--onefile）: 単一 exe を externalBin へコピーする。
  one-folder が Tauri 同梱で不安定な場合の代替。切替時は理由と起動時間を
  docs/reference/desktop_sidecar.ja.md に記録すること。
- build 後、bundle に .env / 実 user config / instances が混入していないかを
  検査し、見つかれば失敗させる（移行計画 §15）。

usage:
  python scripts/build_backend_sidecar.py [--onefile] [--target-triple TRIPLE]
"""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "packaging" / "pyinstaller" / "butly-backend.spec"
DIST_DIR = REPO_ROOT / "packaging" / "pyinstaller" / "dist"
WORK_DIR = REPO_ROOT / "packaging" / "pyinstaller" / "build"
BINARIES_DIR = REPO_ROOT / "frontend" / "src-tauri" / "binaries"
RESOURCES_BACKEND_DIR = (
    REPO_ROOT / "frontend" / "src-tauri" / "resources" / "backend"
)

# bundle に混入してはいけないもの（*.example は許可）
FORBIDDEN_NAMES = {".env", "APIkey.env", "user_config.json", "user_prompts.json",
                   "system_config.json", "external_accounts.json", "persons.json"}


def default_target_triple() -> str:
    try:
        out = subprocess.run(
            ["rustc", "-Vv"], capture_output=True, text=True, check=True
        ).stdout
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.CalledProcessError):
        pass

    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64",
            "aarch64": "aarch64"}.get(machine, machine)
    system = platform.system()
    if system == "Windows":
        return f"{arch}-pc-windows-msvc"
    if system == "Darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-gnu"


def assert_no_secrets(root: Path) -> None:
    problems = []
    for path in root.rglob("*"):
        if path.name in FORBIDDEN_NAMES:
            problems.append(path)
        if path.is_dir() and path.name == "instances" and "butly_core" in path.parts:
            problems.append(path)
    if problems:
        for p in problems:
            print(f"[build] FORBIDDEN file bundled: {p}")
        raise SystemExit("secret / user data files must not be bundled")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", action="store_true")
    parser.add_argument("--target-triple", default=None)
    args = parser.parse_args()

    triple = args.target_triple or default_target_triple()
    exe_suffix = ".exe" if "windows" in triple else ""

    import os

    env = dict(os.environ)
    env["BUTLY_SIDECAR_ONEFILE"] = "1" if args.onefile else "0"

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(SPEC_PATH),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(WORK_DIR),
        ],
        check=True,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert_no_secrets(DIST_DIR)

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    RESOURCES_BACKEND_DIR.mkdir(parents=True, exist_ok=True)
    internal_target = RESOURCES_BACKEND_DIR / "_internal"
    if internal_target.exists():
        shutil.rmtree(internal_target)

    target_exe = BINARIES_DIR / f"butly-backend-{triple}{exe_suffix}"
    if args.onefile:
        src_exe = DIST_DIR / f"butly-backend{exe_suffix}"
        shutil.copy2(src_exe, target_exe)
        # tauri.conf.json の resources 参照を満たすため空 _internal を置く
        internal_target.mkdir(parents=True)
        (internal_target / ".keep").write_text("", encoding="utf-8")
        mode = "one-file"
    else:
        folder = DIST_DIR / "butly-backend"
        src_exe = folder / f"butly-backend{exe_suffix}"
        shutil.copy2(src_exe, target_exe)
        shutil.copytree(folder / "_internal", internal_target)
        mode = "one-folder"

    print(f"[build] mode: {mode}")
    print(f"[build] sidecar exe: {target_exe}")
    print(f"[build] resources:   {internal_target}")


if __name__ == "__main__":
    main()
