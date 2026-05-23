"""
gemini_native.py (protocols)
----------------------------
Gemini native API を喋る Provider Adapter。

Phase 1 では実装本体は ``butly_core.llm.providers.gemini.GeminiProvider``
に残し、ここでは protocols 名前空間で参照できるよう **エイリアス** として
公開する。OpenAICompatAdapter と並列に factory から扱える。

Phase 2/3 でコード本体を物理移動する選択肢を残しつつ、Phase 1 で
無理な大規模移動はしない (リスク最小化)。
"""

from __future__ import annotations

from butly_core.llm.providers.gemini import GeminiProvider


class GeminiNativeAdapter(GeminiProvider):
    """Gemini native protocol Adapter。

    内部実装は ``GeminiProvider`` をそのまま継承する。
    将来 Connection を引数に取れるよう ``__init__`` を拡張済み。
    """

    def __init__(self, connection=None, default_model_name=None):
        """connection / default_model_name は将来用 (Phase 2 で使用)。"""
        super().__init__()
        # 将来 base_url / api_key_env 切替に使う想定。
        # 現在は GeminiProvider が AI_CONFIG / 環境変数を直接見るため未使用。
        self.connection = connection
        self.default_model_name = default_model_name


__all__ = ["GeminiNativeAdapter"]
