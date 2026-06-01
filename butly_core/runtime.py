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
        async for event in ChatService.execute_stream(
            request=request,
            get_instance_components=self.get_instance_components,
            instance_manager=self.instance_manager,
            instances_dir=self.instances_dir,
            gatekeeper=self.gatekeeper,
            mem_block_builder=self.mem_block_builder,
        ):
            yield event
