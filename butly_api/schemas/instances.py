"""
instances.py
────────────
`GET /api/v1/instances` の contract schema（Phase 0 設計版）。

既存実装との接続方針
────────────────────
- legacy `GET /instances` は instance 名の文字列配列を返すが、
  v1 は `InstanceSummary` の list に置き換える。
- 実装時は routers 直下の `INSTANCES_DIR.iterdir()` ではなく、
  application service（InstanceManager / instance config 読み出し）へ委譲し、
  `ai_name` は agent_profile、`locale` は instance config、
  `updated_at` は 記憶ファイルの最終更新から正規化して返す。
- file path を resource ID として返さない。`name` が当面の安定 ID
  （将来 stable ID 導入余地は rename API 側で残す）。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class InstanceSummary(BaseModel):
    """instance 一覧の1件分。"""

    name: str
    ai_name: Optional[str] = None
    #: 会話 UI で送信者ラベルに使うユーザーの呼び名。
    #: user_profile の preferred_call（「呼ばれたい名前」）→ user_name の順で解決した
    #: 表示名だけを返す。誕生日・居住地・性別など他の user_profile 項目は返さない。
    user_display_name: Optional[str] = None
    locale: Optional[str] = None
    updated_at: Optional[datetime] = None
    has_database: bool = False


class InstanceListResponse(BaseModel):
    """`GET /api/v1/instances` の応答。"""

    items: List[InstanceSummary] = Field(default_factory=list)
