import json
from pathlib import Path
from datetime import datetime

from butly_core.io_utils import atomic_write_text


class UsageTracker:
    """月次の検索 API 使用量を追跡する。

    v3.1: provider 別カウント対応 (B6)。
    旧形式 {YYYY-MM: int} と新形式 {YYYY-MM: {tavily: N, ollama: M}} の両方を読める。
    初回更新時に旧形式を新形式へ lazy migration する。
    """

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
        atomic_write_text(cls._file_path, json.dumps(data, indent=2))

    @classmethod
    def _normalize_entry(cls, entry):
        """旧形式 (int) を新形式 (dict) に変換する。

        旧形式の int は tavily のカウントとして扱う。
        """
        if isinstance(entry, int):
            return {"tavily": entry}
        if isinstance(entry, dict):
            return entry
        return {}

    @classmethod
    def increment(cls, provider: str = "tavily"):
        """指定 provider のカウントをインクリメントする。

        Parameters
        ----------
        provider : str
            "tavily" or "ollama" (デフォルト: "tavily")
        """
        data = cls._load()
        key = datetime.now().strftime("%Y-%m")
        entry = cls._normalize_entry(data.get(key, {}))
        entry[provider] = entry.get(provider, 0) + 1
        data[key] = entry
        cls._save(data)

    @classmethod
    def get_current_month_count(cls, provider: str = None) -> int:
        """今月のカウントを返す。

        Parameters
        ----------
        provider : str, optional
            指定時はそのプロバイダーのカウントのみ。
            None の場合は全プロバイダーの合計 (後方互換)。
        """
        data = cls._load()
        key = datetime.now().strftime("%Y-%m")
        entry = data.get(key, 0)

        # 旧形式 (int) の場合
        if isinstance(entry, int):
            if provider is None or provider == "tavily":
                return entry
            return 0

        # 新形式 (dict) の場合
        if isinstance(entry, dict):
            if provider is None:
                return sum(entry.values())
            return entry.get(provider, 0)

        return 0

    @classmethod
    def get_all(cls) -> dict:
        return cls._load()
