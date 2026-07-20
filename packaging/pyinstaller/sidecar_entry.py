"""PyInstaller sidecar 用エントリポイント。

`python -m butly_api.server` と同じ挙動。spec からはこのファイルを入口にする。
"""

from butly_api.server import main

if __name__ == "__main__":
    main()
