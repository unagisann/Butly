"""
runtime.py
----------
ButlyRuntime: チャット実行の中核コンテナ。

FastAPI ルータや Streamlit の都合を知らない「複数入口で共有できる中核」として、
インスタンスコンポーネントの遅延初期化と ChatService 呼び出しをまとめる。

Discord bot / LINE webhook / 将来の CLI / Desktop app などは、HTTP ルータを
import せずに ``runtime.chat(request)`` / ``runtime.chat_stream(request)`` を
呼ぶだけで Butly の応答を得られる。

設計方針:
  - グローバル状態（dependencies.py）をいきなり全廃せず、まずは外部入口から
    扱いやすい小さな実行コンテナを用意する。
  - dependencies.py はこの Runtime のインスタンスを参照する形に寄せる
    （instance_store / instance_manager 等は同一オブジェクトを共有）。
  - 過剰な DI / async 化はしない。個人用ローカルアプリとしての軽さを保つ。
"""

from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import HTTPException

from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder
from butly_core.core.instance_manager import InstanceManager
from butly_core.core.memory import ButlyMemory
from butly_core.chat.service import ChatService
from butly_core.chat.types import ChatRequest, ChatResponse
from butly_core.external.person_registry import PersonRegistry


class ButlyRuntime:
    """
    チャット実行の中核コンテナ。

    保持する依存:
      - data_dir / base_dir / instances_dir : パス類
      - instance_manager : インスタンス CRUD / 設定読み込み
      - gatekeeper       : 文脈分類 + SessionState 更新
      - mem_block_builder: 記憶ブロック構築
      - instance_store   : インスタンスごとの遅延初期化済みコンポーネントのキャッシュ
    """

    def __init__(
        self,
        data_dir: Path,
        base_dir: Optional[Path] = None,
        instances_dir: Optional[Path] = None,
    ):
        self.data_dir: Path = data_dir
        self.base_dir: Path = base_dir if base_dir is not None else data_dir
        self.instances_dir: Path = (
            instances_dir
            if instances_dir is not None
            else self.base_dir / "butly_core" / "instances"
        )

        self.instance_manager: InstanceManager = InstanceManager(self.data_dir)
        self.gatekeeper: Gatekeeper = Gatekeeper(base_dir=self.base_dir)
        self.mem_block_builder: MemoryBlockBuilder = MemoryBlockBuilder()
        # 話者帰属 (persons.json)。外部入口のリクエストに person_id を付与する。
        self.person_registry: PersonRegistry = PersonRegistry(self.data_dir)

        # インスタンスごとのコンポーネント（memory / brain / chronos）の遅延初期化キャッシュ。
        # dependencies.py からも同一オブジェクトとして参照される。
        self.instance_store: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # インスタンスコンポーネント
    # ------------------------------------------------------------------
    def get_instance_components(self, instance_name: str) -> dict:
        """インスタンスのコンポーネントを取得（遅延初期化）。

        存在しないインスタンス名の場合は 404 相当の ``HTTPException`` を送出する
        （既存 FastAPI ルータの挙動を維持するため）。
        """
        if instance_name in self.instance_store:
            return self.instance_store[instance_name]

        if not (self.instances_dir / instance_name).exists():
            raise HTTPException(
                status_code=404, detail=f"Instance '{instance_name}' not found."
            )

        print(f"[System] Initializing instance: {instance_name}")
        memory = ButlyMemory(self.base_dir, instance_name=instance_name)
        brain = ButlyBrain(self.base_dir)
        chronos = ButlyChronos()

        components = {
            "memory": memory,
            "brain": brain,
            "chronos": chronos,
        }
        self.instance_store[instance_name] = components
        return components

    # ------------------------------------------------------------------
    # 話者帰属 (group_context_lanes_plan Phase 1)
    # ------------------------------------------------------------------
    def _attach_person(self, request: ChatRequest) -> None:
        """外部入口のリクエストに person_id を解決して付与する。

        - adapter が既に person_id を設定済み、または外部 ID が無い
          (Web UI 等) 場合は何もしない。
        - 解決失敗時も何もしない。保存時 (_build_turn_meta) に決定的な
          仮 ID へフォールバックするため、帰属自体は失われない。
        - LINE は現行 1:1 スコープのため、未登録 alias の場合は owner とみなす。
        - display_name は adapter のスナップショットを優先し、無い場合のみ
          レジストリの登録名で補完する。
        """
        if request.person_id or not request.external_user_id:
            return
        try:
            resolved = self.person_registry.resolve(
                request.source, request.external_user_id
            )
        except Exception as e:
            print(f"[Runtime] person 解決に失敗 (保存時は仮 ID で継続): {e}")
            return
        if request.source == "line" and resolved.provisional:
            request.person_id = self.person_registry.owner_person_id()
            if not request.external_display_name:
                display_name = self.person_registry.display_name(request.person_id)
                if display_name:
                    request.external_display_name = display_name
            return
        request.person_id = resolved.person_id
        if not request.external_display_name and resolved.display_name:
            request.external_display_name = resolved.display_name

    # ------------------------------------------------------------------
    # チャット実行
    # ------------------------------------------------------------------
    async def chat(
        self,
        request: ChatRequest,
        ws_manager=None,
    ) -> ChatResponse:
        """チャットリクエストを処理して応答を返す（非ストリーミング）。

        外部入口（Discord / LINE 等）はこの 1 メソッドだけ呼べばよい。
        """
        self._attach_person(request)
        return await ChatService.execute(
            request=request,
            get_instance_components=self.get_instance_components,
            instance_manager=self.instance_manager,
            instances_dir=self.instances_dir,
            gatekeeper=self.gatekeeper,
            mem_block_builder=self.mem_block_builder,
            ws_manager=ws_manager,
        )

    async def chat_stream(
        self,
        request: ChatRequest,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """チャットリクエストを処理して逐次 event を yield する（ストリーミング）。"""
        self._attach_person(request)
        async for event in ChatService.execute_stream(
            request=request,
            get_instance_components=self.get_instance_components,
            instance_manager=self.instance_manager,
            instances_dir=self.instances_dir,
            gatekeeper=self.gatekeeper,
            mem_block_builder=self.mem_block_builder,
        ):
            yield event
