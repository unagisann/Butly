import json
from pathlib import Path
from datetime import datetime


class UsageTracker:
    """月次の検索 API 使用量を追跡する"""

    _file_path = Path("butly_core/search_usage.json")

    @classmethod
    def _load(cls) -> dict:
        if cls._file_path.exists():
            try:
                return json.loads(cls._file_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @classmethod
    def _save(cls, data: dict):
        cls._file_path.parent.mkdir(parents=True, exist_ok=True)
        cls._file_path.write_text(json.dumps(data, indent=2))

    @classmethod
    def increment(cls):
        data = cls._load()
        key = datetime.now().strftime("%Y-%m")
        data[key] = data.get(key, 0) + 1
        cls._save(data)

    @classmethod
    def get_current_month_count(cls) -> int:
        data = cls._load()
        key = datetime.now().strftime("%Y-%m")
        return data.get(key, 0)

    @classmethod
    def get_all(cls) -> dict:
        return cls._load()
