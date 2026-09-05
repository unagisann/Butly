import os
import json
import sqlite3
import math
import time
from collections import Counter
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

from butly_core.trace.collector import record_llm_call
from butly_core.core.chronos import resolve_now
from butly_core.core.json_extract import extract_json_str
from butly_core.core import hybrid_search
from butly_core.core.evidence_fusion import (
    DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
    DEFAULT_RAW_CHUNK_CHARS,
    RuntimeEvidenceFusion,
)
from butly_core.llm.embedding_profiles import (
    QUERY,
    apply_prefix as apply_embedding_prefix,
)
from butly_core.settings.memory_retrieval import (
    runtime_memory_retrieval_snapshot,
)

# ★設定ファイルのインポート
try:
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts
except ImportError:
    # パス解決のためのフォールバック (実行環境による)
    import sys

    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts


def _count_retrieval_sources(rows: list) -> dict:
    """候補が vector / bm25 / both のどれ由来かの内訳（trace 用）。"""
    counts = {"vector": 0, "bm25": 0, "both": 0}
    for row in rows:
        source = row.get("retrieval_source")
        if source in counts:
            counts[source] += 1
    return counts


def _decay_basis_datetime(row_dict: dict):
    """time decay の基準日時を返す。

    source_date（元会話の日付 = 出来事の古さ）を優先し、無ければ従来どおり
    created_at（カード作成日時）。どちらも解釈できなければ None（減衰なし）。
    """
    for key, fmt in (
        ("source_date", "%Y-%m-%d"),
        ("created_at", "%Y-%m-%d %H:%M:%S"),
    ):
        value = row_dict.get(key)
        if value:
            try:
                return datetime.strptime(value, fmt)
            except (ValueError, TypeError):
                continue
    return None


class ButlyBrain:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        # Configから読み込む (デフォルト値はConfig依存)
        self.model_name = AI_CONFIG["chat"]["model_name"]

        # db_path はインスタンスごとに _get_db_path() で解決するため、固定値を持たない
        self.instances_dir = self.base_dir / "butly_core" / "instances"
        self.db_name = SYSTEM_CONFIG["paths"]["db_name"]

    def _get_db_path(self, instance_name: str) -> Path:
        """インスタンス名からDBパスを解決"""
        return self.instances_dir / instance_name / self.db_name

    @staticmethod
    def _knowledge_select_cols(cursor) -> str:
        """knowledge_cards の SELECT カラムを DB の実スキーマに合わせて返す。

        source_date / source_files はマイグレーション（ButlyDatabase 初期化）で
        追加される。検索経路は sqlite3 を直接開くため、未マイグレーションの
        既存 DB でも検索が落ちないようカラムの有無を確認する。
        """
        base = (
            "id, title, summary, episode, type, embedding_blob, "
            "created_at, is_archived"
        )
        try:
            columns = {
                row[1]
                for row in cursor.execute("PRAGMA table_info(knowledge_cards)")
            }
            extras = [c for c in ("source_date", "source_files") if c in columns]
            if extras:
                return base + ", " + ", ".join(extras)
        except sqlite3.Error:
            pass
        return base

    # --- Provider アクセス ---
    def _get_provider(self, model=None):
        """Provider を取得する。

        引数として受け付ける形式 (Phase 2):
          - None: デフォルト chat モデル (self.model_name) を使う (旧形式互換)
          - str: 旧 API "gpt-4o" 等
          - dict: 新 API {"connection": "openai", "model_name": "gpt-4o"} or
                 {"model_name": "gpt-4o"} (旧形式)
          - ModelRef: 型安全な参照

        ProviderFactory に正規化を委譲する。
        """
        from butly_core.llm.factory import ProviderFactory

        if model is None:
            model = self.model_name
        return ProviderFactory.create(model)

    def _calculate_cosine_similarity(self, vec1, vec2):
        if vec1 is None or vec2 is None:
            return 0.0
        try:
            # numpy array conversion
            v1 = np.array(vec1)
            v2 = np.array(vec2)
            if v1.shape != v2.shape:
                print(
                    f"[Brain] Dimension mismatch: {v1.shape} vs {v2.shape}. Skipping."
                )
                return 0.0

            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)

            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(np.dot(v1, v2) / (norm1 * norm2))
        except Exception as e:
            print(f"[Brain] Cosine Sim Error: {e}")
            return 0.0

    def _resolve_embedding_conf(self, override_config=None) -> dict:
        """embedding 設定を解決する。

        グローバル AI_CONFIG["embedding"] を基底に、instance/profile 由来の
        override_config["embedding"] で上書きする。これにより QA 経路の
        ベクトル検索も、書き込み(Sleeptime)側と同じ embedding connection を
        使える。override 未指定時はグローバル設定（後方互換）。
        """
        conf = dict(AI_CONFIG.get("embedding", {}))
        if override_config and isinstance(override_config.get("embedding"), dict):
            conf.update(override_config["embedding"])
        return conf

    def get_embedding(self, text, embedding_conf=None, kind=QUERY):
        # Phase 2: connection + model_name の dict 全体を Provider に渡す。
        # Provider 側 (OpenAICompatAdapter) は config["model_name"] を最優先で使う。
        # embedding_conf 未指定時はグローバル AI_CONFIG。instance ごとの
        # embedding を使うには呼び出し側が _resolve_embedding_conf の結果を渡す。
        if embedding_conf is None:
            embedding_conf = AI_CONFIG.get("embedding", {})
        # 検索用モデルはクエリ側と文書側で prefix 規約が異なる (nomic の
        # search_query:/search_document: 等)。付け忘れると埋め込みが潰れて
        # cosine の識別力が落ちるため、モデルに応じた prefix を必ず通す。
        text = apply_embedding_prefix(text, embedding_conf, kind)
        t0 = time.time()
        try:
            provider = self._get_provider(embedding_conf)
            result = provider.embed(text, config=embedding_conf)
            from butly_core.trace.collector import usage_metadata

            record_llm_call(
                purpose="embedding",
                model=embedding_conf.get("model_name", ""),
                connection_id=embedding_conf.get("connection", ""),
                duration_ms=int((time.time() - t0) * 1000),
                prompt_chars=len(text) if text else 0,
                metadata=usage_metadata(provider),
            )
            return result
        except Exception as e:
            print(f"[Brain] Embedding Error: {e}")
            record_llm_call(
                purpose="embedding",
                model=embedding_conf.get("model_name", ""),
                connection_id=embedding_conf.get("connection", ""),
                duration_ms=int((time.time() - t0) * 1000),
                prompt_chars=len(text) if text else 0,
                error=str(e),
            )
            return None

    def _merge_config(self, base_conf, override_conf):
        """Deep merge config dictionaries"""
        if not override_conf:
            return base_conf

        merged = base_conf.copy()
        for key, value in override_conf.items():
            if (
                isinstance(value, dict)
                and key in merged
                and isinstance(merged[key], dict)
            ):
                merged[key] = self._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    def summarize_conversation(
        self, conversation_text: str, override_config=None
    ) -> str:
        """会話ログをサマリーモデルで要約する（memory.maintain_memory から呼ばれる）"""
        from butly_core.prompts import (
            resolve_prompt_locale,
            user_prompt_overrides_enabled,
        )

        locale = resolve_prompt_locale(override_config)
        try:
            summary_conf = AI_CONFIG["summary"].copy()
            if override_config and "summary" in override_config:
                summary_conf = self._merge_config(
                    summary_conf, override_config["summary"]
                )

            brain_conf = SYSTEM_CONFIG["brain"].copy()
            if override_config and "brain" in override_config:
                brain_conf.update(override_config["brain"])

            # プロバイダが model_name / temperature を config から読むため、summary設定をマージ
            merged_conf = brain_conf.copy()
            merged_conf["model_name"] = summary_conf["model_name"]
            # connection も伝搬 (Phase 2: ProviderFactory が ModelRef に解決する)
            if summary_conf.get("connection"):
                merged_conf["connection"] = summary_conf["connection"]
            merged_conf["temperature"] = summary_conf.get("generation_config", {}).get(
                "temperature", 0.3
            )
            merged_conf["locale"] = locale
            merged_conf["allow_user_prompt_overrides"] = (
                user_prompt_overrides_enabled(override_config)
            )

            instance_config = (
                override_config if isinstance(override_config, dict) else {}
            )
            agent_profile = instance_config.get("agent_profile") or {}
            legacy_agent = instance_config.get("agent") or {}
            merged_conf["agent_name"] = (
                agent_profile.get("ai_name")
                or legacy_agent.get("ai_name")
                or SYSTEM_CONFIG["agent"].get("agent_name", "Butly")
            )

            # Phase 2: dict (connection + model_name) を Provider Factory に渡す
            provider = self._get_provider(summary_conf)
            return provider.summarize(conversation_text, merged_conf)
        except Exception as e:
            print(f"[Brain] Summarize Error: {e}")
            return "No summary" if locale != "ja" else "要約なし"

    def extract_keywords(self, user_input, override_config=None):
        """ユーザー発言からDB検索用のキーワードを抽出"""
        try:
            chat_conf = AI_CONFIG["chat"]
            if override_config and "chat" in override_config:
                chat_conf = self._merge_config(chat_conf, override_config["chat"])

            from butly_core.prompts import PromptLoader

            loader = PromptLoader()
            prompt = loader.get("brain_extract_keywords", user_input=user_input)

            # Phase 2: chat_conf (connection + model_name) を Provider Factory に渡す
            provider = self._get_provider(chat_conf)
            t0 = time.time()
            call_error = None
            try:
                text = provider.classify(prompt, chat_conf)
            except Exception as call_e:
                call_error = str(call_e)
                raise
            finally:
                # LLM 呼び出し自体の成否だけを記録する (後続の JSON パース失敗は
                # 呼び出し失敗ではないため、ここで finally 記録する)
                from butly_core.trace.collector import usage_metadata

                record_llm_call(
                    purpose="keyword_extract",
                    model=chat_conf.get("model_name", ""),
                    connection_id=chat_conf.get("connection", ""),
                    duration_ms=int((time.time() - t0) * 1000),
                    prompt_chars=len(prompt),
                    error=call_error,
                    metadata=usage_metadata(provider),
                )
            text = text.strip() if text else ""
            print(f"[Brain] Raw Keyword Response: {text}")

            return json.loads(extract_json_str(text))
        except Exception as e:
            print(f"[Brain] Keyword Extraction Error: {e}")
            return {"keywords": []}

    def quick_vector_search(
        self,
        user_input: str,
        instance_name: str,
        limit: int = 3,
        threshold: float = 0.6,
        override_config: dict = None,
        retrieval_query: str = None,
    ) -> list:
        """既存 API: 結果リストのみを返す (後方互換)。"""
        return self.quick_vector_search_diag(
            user_input,
            instance_name,
            limit,
            threshold,
            override_config,
            retrieval_query,
        )["results"]

    def quick_vector_search_diag(
        self,
        user_input: str,
        instance_name: str,
        limit: int = 3,
        threshold: float = 0.6,
        override_config: dict = None,
        retrieval_query: str = None,
        query_embedding=None,
    ) -> dict:
        """
        診断情報付きベクトル検索。

        ``query_embedding``はoffline二段検索が同じ質問vectorを再利用するための
        任意入力。未指定時の本番経路は従来どおり内部でembeddingする。
        Returns:
            {
                "results": [...],
                "diagnostics": {
                    "threshold": float,
                    "decay_rate": float,
                    "fetch_limit": None,
                    "fetched_count": int,
                    "passed_threshold": int,
                    "top_raw_scores": [...],
                    "top_final_scores": [...],
                    "target_instances": [...]
                }
            }
        """
        brain_conf = SYSTEM_CONFIG["brain"].copy()
        if override_config and "brain" in override_config:
            brain_conf.update(override_config["brain"])
        embedding_conf = self._resolve_embedding_conf(override_config)
        reranker_config, reranker_config_error = self._resolve_reranker_config(
            override_config
        )
        post_rank_limit = (
            max(limit, reranker_config.candidate_limit)
            if reranker_config is not None
            else limit
        )

        target_instances = self._resolve_target_instances(instance_name, brain_conf)

        if brain_conf.get("search_mode") == "dual_query":
            output = self._dual_query_search_diag(
                user_input,
                retrieval_query,
                target_instances,
                limit=post_rank_limit,
                threshold=threshold,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
            )
            return self._finalize_retrieval(
                user_input,
                output,
                limit=limit,
                override_config=override_config,
                reranker_config=reranker_config,
                reranker_config_error=reranker_config_error,
            )

        if brain_conf.get("search_mode") == "hybrid_evidence_fusion":
            output = self._hybrid_evidence_fusion_search_diag(
                user_input,
                target_instances,
                default_instance=instance_name,
                limit=post_rank_limit,
                threshold=threshold,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
                query_embedding=query_embedding,
            )
            return self._finalize_retrieval(
                user_input,
                output,
                limit=limit,
                override_config=override_config,
                reranker_config=reranker_config,
                reranker_config_error=reranker_config_error,
            )

        if brain_conf.get("search_mode") == "hybrid":
            output = self._hybrid_search_diag(
                user_input,
                target_instances,
                limit=post_rank_limit,
                threshold=threshold,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
                query_embedding=query_embedding,
            )
            return self._finalize_retrieval(
                user_input,
                output,
                limit=limit,
                override_config=override_config,
                reranker_config=reranker_config,
                reranker_config_error=reranker_config_error,
            )

        t0 = time.time()
        # 上位 limit 件だけを返す一方、A/B とリコール計測のために候補列
        # （hybrid 側の vector_candidates と同じ深さ）まで順位を残す
        if reranker_config is not None:
            candidate_limit = max(limit, reranker_config.candidate_limit)
        else:
            candidate_limit = max(
                limit, int(brain_conf.get("vector_candidates", 20))
            )
        all_results = []
        all_raw_scores: list = []
        all_final_scores: list = []
        total_fetched = 0
        for inst in target_instances:
            single = self._quick_vector_search_single_diag(
                user_input,
                inst,
                candidate_limit,
                threshold,
                brain_conf,
                embedding_conf,
                query_embedding=query_embedding,
            )
            # usage_count 用に source_instance を付与（複数 instance 横断時の DB 振り分けに使う）
            for r in single["results"]:
                r["source_instance"] = inst
            all_results.extend(single["results"])
            all_raw_scores.extend(single["raw_scores"])
            all_final_scores.extend(single["final_scores"])
            total_fetched += single["fetched_count"]

        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        candidates = all_results[:candidate_limit]

        all_raw_scores.sort(reverse=True)
        all_final_scores.sort(reverse=True)

        candidate_ids = [str(r.get("id")) for r in candidates]
        diagnostics = {
            "mode": "vector",
            "threshold": float(threshold),
            "decay_rate": float(brain_conf.get("time_decay_rate", 0.005)),
            # Kept for trace compatibility. Pure vector search scores the full
            # card set; fallback_fetch_limit only applies to keyword fallback.
            "fetch_limit": None,
            "fetched_count": total_fetched,
            "passed_threshold": len(all_results),
            "top_raw_scores": [round(s, 3) for s in all_raw_scores[:5]],
            "top_final_scores": [round(s, 3) for s in all_final_scores[:5]],
            "target_instances": target_instances,
            "vector_candidate_ids": candidate_ids,
            "bm25_candidate_ids": [],
            "fused_candidate_ids": candidate_ids,
            "latency_ms": int((time.time() - t0) * 1000),
        }
        return self._finalize_retrieval(
            user_input,
            {"results": candidates, "diagnostics": diagnostics},
            limit=limit,
            override_config=override_config,
            reranker_config=reranker_config,
            reranker_config_error=reranker_config_error,
        )

    @staticmethod
    def _resolve_reranker_config(override_config):
        try:
            from butly_core.core.reranker import RerankerConfig

            return (
                RerankerConfig.from_mapping(
                    (override_config or {}).get("reranker")
                ),
                None,
            )
        except Exception as exc:
            return None, str(exc)

    @staticmethod
    def _candidate_identity(row: dict) -> tuple[str, str]:
        return str(row.get("source_instance") or ""), str(row.get("id"))

    def _validate_reranker_output(
        self,
        candidates: list[dict],
        reranked: dict,
        reranker_config,
    ) -> tuple[list[dict], list[str]]:
        from butly_core.core.reranker import RerankerError

        if not isinstance(reranked, dict):
            raise RerankerError("reranker returned a non-mapping result")
        rows = reranked.get("results")
        selected_ids = reranked.get("selected_ids")
        if not isinstance(rows, list) or not rows:
            raise RerankerError("reranker returned an empty result order")
        if not isinstance(selected_ids, list):
            raise RerankerError("reranker selected_ids must be a list")
        if any(not isinstance(row, dict) for row in rows):
            raise RerankerError("reranker results must contain mappings")
        input_ids = Counter(self._candidate_identity(row) for row in candidates)
        output_ids = Counter(self._candidate_identity(row) for row in rows)
        if input_ids != output_ids:
            raise RerankerError(
                "reranker result order must contain every input candidate exactly once"
            )
        normalized_selected_ids = [str(value) for value in selected_ids]
        candidate_ids = {str(row.get("id")) for row in candidates}
        if (
            len(normalized_selected_ids) != len(set(normalized_selected_ids))
            or any(value not in candidate_ids for value in normalized_selected_ids)
        ):
            raise RerankerError(
                "reranker selected_ids must be unique input candidate IDs"
            )
        if (
            not selected_ids
            and not (
                reranker_config.engine == "cross_encoder"
                and reranker_config.score_threshold is not None
            )
        ):
            raise RerankerError("reranker returned no selected candidates")
        return rows, normalized_selected_ids

    def _finalize_retrieval(
        self,
        query: str,
        output: dict,
        *,
        limit: int,
        override_config: dict = None,
        reranker_config=None,
        reranker_config_error: str = None,
    ) -> dict:
        candidates = list(output.get("results") or [])
        diagnostics = dict(output.get("diagnostics") or {})
        pre_reranker_ids = [str(row.get("id")) for row in candidates]
        configured_fused_ids = list(
            diagnostics.get("fused_candidate_ids") or pre_reranker_ids
        )
        diagnostics["settings"] = runtime_memory_retrieval_snapshot(
            SYSTEM_CONFIG,
            override_config,
        )
        diagnostics["settings"]["vector_search_limit"] = limit
        diagnostics.setdefault("reranked_candidate_ids", [])

        if reranker_config_error is not None:
            diagnostics["effective_candidate_ids"] = configured_fused_ids
            diagnostics["reranker"] = {
                "enabled": True,
                "status": "error",
                "fallback": True,
                "model_name": None,
                "latency_ms": None,
                "selected_count": 0,
                "error": reranker_config_error,
            }
            return {"results": candidates[:limit], "diagnostics": diagnostics}

        if reranker_config is None:
            diagnostics["effective_candidate_ids"] = configured_fused_ids
            return {"results": candidates[:limit], "diagnostics": diagnostics}

        diagnostics["fused_candidate_ids"] = pre_reranker_ids
        reranker_diag = {
            "enabled": True,
            "status": "error",
            "fallback": True,
            "engine": reranker_config.engine,
            "model_name": reranker_config.model_name,
            "connection": reranker_config.connection,
            "score_threshold": reranker_config.score_threshold,
            "candidate_count": len(candidates),
            "selected_count": 0,
            "latency_ms": None,
            "error": None,
        }
        if not candidates:
            reranker_diag.update(
                {
                    "status": "skipped",
                    "fallback": False,
                    "reason": "no_candidates",
                    "latency_ms": 0,
                }
            )
            diagnostics["effective_candidate_ids"] = []
            diagnostics["reranker"] = reranker_diag
            return {"results": [], "diagnostics": diagnostics}

        effective_candidates = candidates
        effective_limit = min(limit, len(candidates))
        try:
            reranker = self._get_reranker(reranker_config)
            reranked = reranker.rerank(
                query,
                candidates,
                top_n=effective_limit,
            )
            effective_candidates, selected_ids = self._validate_reranker_output(
                candidates,
                reranked,
                reranker_config,
            )
            effective_limit = len(selected_ids)
            reranker_diag.update(
                {
                    "status": "completed",
                    "fallback": False,
                    "selected_count": len(selected_ids),
                    "selected_candidate_ids": selected_ids,
                    "scores": reranked.get("scores") or [],
                    "latency_ms": reranked.get("latency_ms"),
                    "token_usage": reranked.get("token_usage"),
                    "completion_metadata": reranked.get(
                        "completion_metadata"
                    ),
                }
            )
        except Exception as exc:
            reranker_diag.update(
                {
                    "error": str(exc),
                    "latency_ms": getattr(exc, "latency_ms", None),
                    "token_usage": getattr(exc, "token_usage", None),
                    "completion_metadata": getattr(
                        exc,
                        "completion_metadata",
                        None,
                    ),
                }
            )
        effective_ids = [str(row.get("id")) for row in effective_candidates]
        diagnostics["reranked_candidate_ids"] = (
            effective_ids if reranker_diag["status"] == "completed" else []
        )
        diagnostics["effective_candidate_ids"] = effective_ids
        diagnostics["reranker"] = reranker_diag
        return {
            "results": effective_candidates[:effective_limit],
            "diagnostics": diagnostics,
        }

    def _get_reranker(self, config):
        """Create the selected local or provider-backed reranker."""
        from butly_core.core.reranker import create_reranker

        return create_reranker(config)

    def _dual_query_search_diag(
        self,
        original_query: str,
        retrieval_query: str,
        target_instances: list[str],
        *,
        limit: int,
        threshold: float,
        brain_conf: dict,
        embedding_conf: dict = None,
    ) -> dict:
        """Fuse original and Gatekeeper-rewritten vector rankings with RRF.

        The online contract is deliberately bounded: each query contributes at
        most 15 candidates by default, the deduplicated diagnostic pool retains
        at most 25, and the caller still receives only its requested top-k
        (normally three cards for prompt injection).
        """
        t0 = time.time()
        candidate_limit = max(
            1, int(brain_conf.get("dual_query_candidates", 15))
        )
        pool_limit = max(1, int(brain_conf.get("dual_query_pool_limit", 25)))
        rrf_k = max(1, int(brain_conf.get("rrf_k", 60)))

        original = self._vector_query_candidates(
            original_query,
            target_instances,
            limit=candidate_limit,
            threshold=threshold,
            brain_conf=brain_conf,
            embedding_conf=embedding_conf,
        )
        rewritten_text = self._normalize_retrieval_query(retrieval_query)
        original_normalized = self._normalize_retrieval_query(original_query)
        rewrite_executed = bool(
            rewritten_text
            and rewritten_text.casefold() != original_normalized.casefold()
        )
        if rewrite_executed:
            rewritten = self._vector_query_candidates(
                rewritten_text,
                target_instances,
                limit=candidate_limit,
                threshold=threshold,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
            )
            fused = self._fuse_dual_query_rankings(
                original["results"],
                rewritten["results"],
                k=rrf_k,
                limit=pool_limit,
            )
            status = "completed"
            reason = None
        else:
            rewritten = {
                "results": [],
                "raw_scores": [],
                "final_scores": [],
                "fetched_count": 0,
            }
            fused = []
            for rank, row in enumerate(original["results"][:pool_limit], start=1):
                item = dict(row)
                item["original_rank"] = rank
                item["retrieval_query_rank"] = None
                item["original_vector_score"] = item.get("score")
                item["retrieval_query_vector_score"] = None
                item["query_source"] = "original"
                item["retrieval_source"] = "vector"
                fused.append(item)
            status = "fallback"
            reason = "missing_or_same_query"

        original_ids = [str(row.get("id")) for row in original["results"]]
        rewritten_ids = [str(row.get("id")) for row in rewritten["results"]]
        fused_ids = [str(row.get("id")) for row in fused]
        all_raw_scores = original["raw_scores"] + rewritten["raw_scores"]
        all_final_scores = original["final_scores"] + rewritten["final_scores"]
        all_raw_scores.sort(reverse=True)
        all_final_scores.sort(reverse=True)
        overlap = len(set(original_ids) & set(rewritten_ids))
        diagnostics = {
            "mode": "dual_query",
            "threshold": float(threshold),
            "decay_rate": float(brain_conf.get("time_decay_rate", 0.005)),
            "fetch_limit": None,
            "fetched_count": (
                original["fetched_count"] + rewritten["fetched_count"]
            ),
            "passed_threshold": len(fused),
            "top_raw_scores": [round(score, 3) for score in all_raw_scores[:5]],
            "top_final_scores": [
                round(score, 3) for score in all_final_scores[:5]
            ],
            "target_instances": target_instances,
            # vector_candidate_ids remains the original-query baseline for old
            # scorers and vector-only rescue metrics.
            "vector_candidate_ids": original_ids,
            "original_candidate_ids": original_ids,
            "retrieval_query_candidate_ids": rewritten_ids,
            "bm25_candidate_ids": [],
            "fused_candidate_ids": fused_ids,
            "effective_candidate_ids": fused_ids,
            "retrieval_query": rewritten_text if rewrite_executed else None,
            "rrf_k": rrf_k,
            "query_fusion": {
                "status": status,
                "reason": reason,
                "executed": rewrite_executed,
                "candidate_limit_per_query": candidate_limit,
                "pool_limit": pool_limit,
                "original_count": len(original_ids),
                "retrieval_query_count": len(rewritten_ids),
                "overlap_count": overlap,
                "unique_count": len(fused_ids),
            },
            "latency_ms": int((time.time() - t0) * 1000),
        }
        return {"results": fused[:limit], "diagnostics": diagnostics}

    def _vector_query_candidates(
        self,
        query: str,
        target_instances: list[str],
        *,
        limit: int,
        threshold: float,
        brain_conf: dict,
        embedding_conf: dict = None,
    ) -> dict:
        """Collect one global vector ranking across readable instances."""
        rows: list[dict] = []
        raw_scores: list[float] = []
        final_scores: list[float] = []
        fetched_count = 0
        for instance_name in target_instances:
            single = self._quick_vector_search_single_diag(
                query,
                instance_name,
                limit,
                threshold,
                brain_conf,
                embedding_conf,
            )
            for row in single["results"]:
                row["source_instance"] = instance_name
            rows.extend(single["results"])
            raw_scores.extend(single["raw_scores"])
            final_scores.extend(single["final_scores"])
            fetched_count += single["fetched_count"]
        rows.sort(key=lambda row: row.get("score", 0.0), reverse=True)
        return {
            "results": rows[:limit],
            "raw_scores": raw_scores,
            "final_scores": final_scores,
            "fetched_count": fetched_count,
        }

    @staticmethod
    def _normalize_retrieval_query(value: str) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split()).strip()[:500]

    @staticmethod
    def _fuse_dual_query_rankings(
        original_rows: list[dict],
        retrieval_query_rows: list[dict],
        *,
        k: int,
        limit: int,
    ) -> list[dict]:
        """Equal-weight RRF for two vector rankings without BM25 semantics."""
        fused: dict[tuple, dict] = {}

        def merge(rows: list[dict], source: str, rank_field: str) -> None:
            score_field = f"{source}_vector_score"
            for rank, row in enumerate(rows, start=1):
                key = (row.get("source_instance"), row.get("id"))
                entry = fused.get(key)
                if entry is None:
                    entry = dict(row)
                    entry["rrf_score"] = 0.0
                    entry["query_source"] = source
                    entry["retrieval_source"] = "vector"
                    fused[key] = entry
                elif entry["query_source"] != source:
                    entry["query_source"] = "both"
                entry[rank_field] = rank
                entry[score_field] = row.get("score")
                entry["rrf_score"] += 1.0 / (k + rank)

        merge(original_rows, "original", "original_rank")
        merge(
            retrieval_query_rows,
            "retrieval_query",
            "retrieval_query_rank",
        )
        results = list(fused.values())
        for row in results:
            row["score"] = row["rrf_score"]
            row.setdefault("original_rank", None)
            row.setdefault("retrieval_query_rank", None)
            row.setdefault("original_vector_score", None)
            row.setdefault("retrieval_query_vector_score", None)
        results.sort(
            key=lambda row: (
                -row["rrf_score"],
                min(
                    row.get("original_rank") or 10**6,
                    row.get("retrieval_query_rank") or 10**6,
                ),
                row.get("original_rank") or 10**6,
                row.get("retrieval_query_rank") or 10**6,
                str(row.get("id")),
            )
        )
        return results[:limit]

    @staticmethod
    def _resolve_target_instances(instance_name: str, brain_conf: dict) -> list:
        """readable_instances を実インスタンス名へ解決する（"self" 展開・重複除去）。"""
        readable = brain_conf.get("readable_instances", ["self"])
        resolved = [instance_name if r == "self" else r for r in readable]
        return list(dict.fromkeys(resolved))

    # --- ハイブリッド検索（BM25 + ベクトル / RRF 融合。計画書 §3.2） ---

    def _hybrid_search_diag(
        self,
        user_input: str,
        target_instances: list,
        *,
        limit: int,
        threshold: Optional[float],
        brain_conf: dict,
        embedding_conf: dict = None,
        query_embedding=None,
    ) -> dict:
        """BM25 とベクトルの候補をグローバル順位で RRF 融合する。

        threshold=None ならベクトル側の閾値ゲートを外す（Deep 経路）。
        複数インスタンスを跨ぐときは、インスタンスごとの順位をそのまま融合すると
        「どの DB の1位も同じ重み」になってしまうため、両経路とも全インスタンス
        分を集めてからグローバル順位を付ける。
        """
        t0 = time.time()
        spec = hybrid_search.build_fts_query(user_input)
        vector_limit = int(brain_conf.get("vector_candidates", 20))
        bm25_limit = int(brain_conf.get("bm25_candidates", 20))
        vector_threshold = -1.0 if threshold is None else float(threshold)

        vector_rows: list = []
        bm25_rows: list = []
        bm25_diags: dict = {}
        raw_scores: list = []
        final_scores: list = []
        fetched = 0

        for inst in target_instances:
            single = self._quick_vector_search_single_diag(
                user_input,
                inst,
                vector_limit,
                vector_threshold,
                brain_conf,
                embedding_conf,
                query_embedding=query_embedding,
            )
            for r in single["results"]:
                r["source_instance"] = inst
                # cosine は score から退避する。score は RRF スコアで上書きされ、
                # 下流が score 降順で並べ直しても融合順位が壊れないようにする。
                r["vector_score"] = r.get("score")
            vector_rows.extend(single["results"])
            raw_scores.extend(single["raw_scores"])
            final_scores.extend(single["final_scores"])
            fetched += single["fetched_count"]

            bm25 = self._bm25_search_single(spec, inst, bm25_limit, brain_conf)
            for r in bm25["results"]:
                r["source_instance"] = inst
                r["source"] = "bm25"
            bm25_rows.extend(bm25["results"])
            bm25_diags[inst] = bm25["diagnostics"]

        vector_rows.sort(key=lambda r: r.get("vector_score") or 0.0, reverse=True)
        vector_rows = vector_rows[:vector_limit]
        bm25_rows.sort(key=hybrid_search.bm25_sort_key)
        bm25_rows = bm25_rows[:bm25_limit]
        for rank, row in enumerate(bm25_rows, start=1):
            row["bm25_rank"] = rank

        candidate_limit = max(limit, vector_limit, bm25_limit)
        fused = hybrid_search.rrf_fuse(
            vector_rows,
            bm25_rows,
            k=int(brain_conf.get("rrf_k", 60)),
            limit=candidate_limit,
        )
        results = fused[:limit]

        raw_scores.sort(reverse=True)
        final_scores.sort(reverse=True)
        diagnostics = {
            "mode": "hybrid",
            "threshold": vector_threshold,
            "decay_rate": float(brain_conf.get("time_decay_rate", 0.005)),
            "fetch_limit": None,
            "fetched_count": fetched,
            "passed_threshold": len(vector_rows),
            "top_raw_scores": [round(s, 3) for s in raw_scores[:5]],
            "top_final_scores": [round(s, 3) for s in final_scores[:5]],
            "target_instances": target_instances,
            "rrf_k": int(brain_conf.get("rrf_k", 60)),
            # bm25_rescue_rate（BM25 が無ければ届かなかった率）を後から計算できる
            # よう、融合前の2つのランキングをそのまま残す
            "vector_candidate_ids": [str(r.get("id")) for r in vector_rows],
            "bm25_candidate_ids": [str(r.get("id")) for r in bm25_rows],
            "fused_candidate_ids": [str(r.get("id")) for r in fused],
            "retrieval_sources": _count_retrieval_sources(fused),
            "bm25": bm25_diags,
            "latency_ms": int((time.time() - t0) * 1000),
        }
        return {"results": results, "diagnostics": diagnostics}

    def _hybrid_evidence_fusion_search_diag(
        self,
        user_input: str,
        target_instances: list[str],
        *,
        default_instance: str,
        limit: int,
        threshold: Optional[float],
        brain_conf: dict,
        embedding_conf: dict,
        query_embedding=None,
    ) -> dict:
        """Hybrid top-N followed by lazy Episode/RAW evidence rank fusion.

        Evidence failures never make chat fail: the original hybrid ranking is
        returned and diagnostics explicitly mark the fallback.
        """
        started = time.monotonic()
        candidate_limit = max(
            limit,
            int(brain_conf.get("vector_candidates", 20)),
            int(brain_conf.get("bm25_candidates", 20)),
        )
        weight = float(
            brain_conf.get(
                "evidence_fusion_base_weight",
                DEFAULT_EVIDENCE_FUSION_BASE_WEIGHT,
            )
        )
        raw_chunk_chars = int(
            brain_conf.get("evidence_raw_chunk_chars", DEFAULT_RAW_CHUNK_CHARS)
        )
        raw_cache_path = brain_conf.get("evidence_cache_path")
        if raw_cache_path:
            cache_path = Path(str(raw_cache_path)).expanduser()
            if not cache_path.is_absolute():
                cache_path = self.base_dir / cache_path
        else:
            cache_path = (
                self.instances_dir
                / default_instance
                / "retrieval_cache"
                / "evidence_embeddings.sqlite3"
            )
        locale = str(brain_conf.get("locale") or "")
        if locale not in {"en", "ja"}:
            locale = "en"

        fusion = None
        base_output = None
        try:
            fusion = RuntimeEvidenceFusion(
                self.instances_dir,
                embedding_conf,
                cache_path=cache_path,
                raw_chunk_chars=raw_chunk_chars,
                locale=locale,
            )
            if query_embedding is None:
                query_embedding = fusion.embed_query(user_input)
            base_output = self._hybrid_search_diag(
                user_input,
                target_instances,
                limit=candidate_limit,
                threshold=threshold,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
                query_embedding=query_embedding,
            )
            base_results = list(base_output.get("results") or [])
            fusion_diag = fusion.rerank(
                user_input,
                base_results,
                default_instance=default_instance,
                top_n=limit,
                base_weight=weight,
                query_vector=query_embedding,
            )
            cache_diag = fusion.cache.diagnostics()
            fusion_diag["cache"] = {
                key: cache_diag.get(key)
                for key in ("hits", "misses", "writes", "errors", "by_kind")
            }
            by_id = {str(row.get("id")): row for row in base_results}
            score_by_id = {
                str(row.get("card_id")): row
                for row in fusion_diag.get("fusion_scores") or []
            }
            reordered = []
            for card_id in fusion_diag.get("candidate_ids") or []:
                source = by_id.get(str(card_id))
                if source is None:
                    continue
                row = dict(source)
                score = score_by_id.get(str(card_id)) or {}
                row["hybrid_score"] = row.get("rrf_score", row.get("score"))
                row["evidence_score"] = score.get("evidence_score")
                row["fusion_score"] = score.get("fusion_score")
                if score.get("fusion_score") is not None:
                    row["score"] = score["fusion_score"]
                reordered.append(row)

            diagnostics = dict(base_output.get("diagnostics") or {})
            base_ids = [str(row.get("id")) for row in base_results]
            effective_ids = [str(row.get("id")) for row in reordered]
            diagnostics.update(
                {
                    "mode": "hybrid_evidence_fusion",
                    "hybrid_candidate_ids": base_ids,
                    "evidence_candidate_ids": list(
                        fusion_diag.get("evidence_candidate_ids") or []
                    ),
                    "fused_candidate_ids": effective_ids,
                    "effective_candidate_ids": effective_ids,
                    "evidence_fusion_scores": list(
                        fusion_diag.get("fusion_scores") or []
                    ),
                    "evidence_fusion": {
                        **fusion_diag,
                        # Candidate order and scores already have dedicated
                        # fields above; avoid duplicating a large trace payload.
                        "candidate_ids": None,
                        "scores": None,
                        "fusion_scores": None,
                        "base_weight": weight,
                        "raw_chunk_chars": raw_chunk_chars,
                    },
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return {"results": reordered[:limit], "diagnostics": diagnostics}
        except Exception as exc:
            if base_output is None:
                base_output = self._hybrid_search_diag(
                    user_input,
                    target_instances,
                    limit=candidate_limit,
                    threshold=threshold,
                    brain_conf=brain_conf,
                    embedding_conf=embedding_conf,
                )
            results = list(base_output.get("results") or [])
            diagnostics = dict(base_output.get("diagnostics") or {})
            base_ids = [str(row.get("id")) for row in results]
            diagnostics.update(
                {
                    "mode": "hybrid_evidence_fusion",
                    "hybrid_candidate_ids": base_ids,
                    "evidence_candidate_ids": [],
                    "fused_candidate_ids": base_ids,
                    "effective_candidate_ids": base_ids,
                    "evidence_fusion_scores": [],
                    "evidence_fusion": {
                        "status": "fallback",
                        "fallback": True,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                        "candidate_count": len(results),
                        "scored_count": 0,
                        "base_weight": weight,
                        "raw_chunk_chars": raw_chunk_chars,
                        "latency_ms": int(
                            (time.monotonic() - started) * 1000
                        ),
                    },
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return {"results": results[:limit], "diagnostics": diagnostics}
        finally:
            if fusion is not None:
                fusion.close()

    def _bm25_search_single(
        self,
        spec,
        instance_name: str,
        limit: int,
        brain_conf: dict,
    ) -> dict:
        """単一インスタンス DB の BM25 候補。索引が無ければ空を返す。"""
        empty = {"results": [], "diagnostics": {"reason": "unavailable"}}
        instance_db_path = self._get_db_path(instance_name)
        if not instance_db_path.exists():
            return empty
        if spec.is_empty():
            return {"results": [], "diagnostics": {"reason": "no_terms"}}

        conn = None
        try:
            conn = sqlite3.connect(instance_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            if not hybrid_search.fts_index_ready(conn):
                # 索引は ButlyDatabase の初期化で作られるが、hybrid を有効にした
                # 直後の既存 DB にはまだ無い。読み取り経路から一度だけ作る。
                status = hybrid_search.ensure_fts_index(conn)
                conn.commit()
                if not status.get("available"):
                    return {
                        "results": [],
                        "diagnostics": {"reason": status.get("reason", "unavailable")},
                    }
            cursor = conn.cursor()
            columns = self._knowledge_select_cols(cursor).split(",")
            return hybrid_search.bm25_candidates(
                conn,
                spec,
                columns=columns,
                limit=limit,
                weights=brain_conf.get("bm25_weights"),
                max_df_ratio=float(brain_conf.get("bm25_max_df_ratio", 0.5)),
                min_weak_df=int(brain_conf.get("bm25_min_weak_df", 5)),
                scan_limit=int(brain_conf.get("bm25_scan_limit", 500)),
            )
        except sqlite3.Error as e:
            print(f"[Brain] BM25 Search Error ({instance_name}): {e}")
            return {"results": [], "diagnostics": {"reason": f"error:{e}"}}
        finally:
            if conn is not None:
                conn.close()

    def _quick_vector_search_single(
        self,
        user_input: str,
        instance_name: str,
        limit: int,
        threshold: float,
        brain_conf: dict,
    ) -> list:
        """後方互換: 結果リストのみを返すラッパー。"""
        return self._quick_vector_search_single_diag(
            user_input,
            instance_name,
            limit,
            threshold,
            brain_conf,
        )["results"]

    def _quick_vector_search_single_diag(
        self,
        user_input: str,
        instance_name: str,
        limit: int,
        threshold: float,
        brain_conf: dict,
        embedding_conf: dict = None,
        query_embedding=None,
    ) -> dict:
        """単一インスタンスDBに対する純粋ベクトル検索 + 診断情報。

        Returns:
            {
                "results": [...],
                "raw_scores": [全カードの raw cosine スコア],
                "final_scores": [decay/archive 適用後のスコア],
                "fetched_count": int
            }
        """
        empty = {
            "results": [],
            "raw_scores": [],
            "final_scores": [],
            "fetched_count": 0,
        }
        instance_db_path = self._get_db_path(instance_name)
        if not instance_db_path.exists():
            return empty

        if query_embedding is None:
            query_embedding = self.get_embedding(user_input, embedding_conf)
        if query_embedding is None or np.asarray(query_embedding).size == 0:
            return empty

        try:
            conn = sqlite3.connect(instance_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            select_cols = self._knowledge_select_cols(cursor)
            query = f"""
                SELECT {select_cols}
                FROM knowledge_cards
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return empty

            decay_rate = brain_conf.get("time_decay_rate", 0.005)
            now = resolve_now()
            scored_results = []
            raw_scores: list = []
            final_scores: list = []

            for row in rows:
                row_dict = dict(row)
                blob_data = row_dict.pop("embedding_blob")
                is_archived = row_dict.get("is_archived") or 0

                score = 0.0
                raw_score = 0.0
                if blob_data:
                    try:
                        db_emb = np.frombuffer(blob_data, dtype=np.float32)
                        raw_score = float(
                            self._calculate_cosine_similarity(query_embedding, db_emb)
                        )
                        score = raw_score

                        basis_dt = _decay_basis_datetime(row_dict)
                        if basis_dt is not None:
                            days_diff = (now - basis_dt).days
                            if days_diff > 0:
                                decay_factor = math.exp(-decay_rate * days_diff)
                                score *= decay_factor

                        if is_archived:
                            score *= 0.5
                    except Exception:
                        pass

                raw_scores.append(raw_score)
                final_scores.append(score)

                if score >= threshold:
                    row_dict["score"] = float(score)
                    row_dict["source"] = "vector"
                    scored_results.append(row_dict)

            scored_results.sort(key=lambda x: x["score"], reverse=True)
            return {
                "results": scored_results[:limit],
                "raw_scores": raw_scores,
                "final_scores": final_scores,
                "fetched_count": len(rows),
            }

        except Exception as e:
            # 契約は diag dict。list を返すと呼び出し側の single["results"] が
            # TypeError になるため、失敗時も同じ形で返す
            print(f"[Brain] Quick Vector Search Error: {e}")
            return empty

    def search_knowledge(
        self,
        keywords,
        user_query,
        instance_name="00_master",
        limit=None,
        override_config=None,
    ):
        """
        Layer 2（Deep）検索。readable_instances に基づき複数DB横断検索に対応。

        search_mode="vector": 従来どおり keywords の LIKE 絞り込み + cosine 再ランク。
        search_mode="hybrid": keywords（LLM 抽出）は使わず、質問文から決定論的に
          作った検索語で BM25 + ベクトルを RRF 融合する。Layer 1 と違い
          ベクトル側の閾値ゲートを外す（Layer 1 が空だったときの救済という
          Deep の役割を保つため）。keywords は None を許容する。
        search_mode="hybrid_evidence_fusion": 同じhybrid候補をEpisode/RAWの
          Evidence順位と融合する。Evidence障害時はhybridへfallbackする。
        """
        brain_conf = SYSTEM_CONFIG["brain"].copy()
        if override_config and "brain" in override_config:
            brain_conf.update(override_config["brain"])
        embedding_conf = self._resolve_embedding_conf(override_config)

        if limit is None:
            limit = brain_conf["search_limit"]

        target_instances = self._resolve_target_instances(instance_name, brain_conf)

        if brain_conf.get("search_mode") == "hybrid_evidence_fusion":
            return self._hybrid_evidence_fusion_search_diag(
                user_query,
                target_instances,
                default_instance=instance_name,
                limit=limit,
                threshold=None,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
            )["results"]

        if brain_conf.get("search_mode") == "hybrid":
            return self._hybrid_search_diag(
                user_query,
                target_instances,
                limit=limit,
                threshold=None,
                brain_conf=brain_conf,
                embedding_conf=embedding_conf,
            )["results"]

        # 単一DBの場合（高速パス）
        if len(target_instances) == 1:
            return self._search_single_db(
                keywords, user_query, target_instances[0], limit, brain_conf,
                embedding_conf,
            )

        # 複数DBの場合: 各DBに個別クエリ → マージしてリランキング
        return self._search_multi_db(
            keywords, user_query, target_instances, limit, brain_conf,
            embedding_conf,
        )

    def _search_multi_db(
        self, keywords, user_query, instances, limit, brain_conf,
        embedding_conf=None,
    ):
        """複数インスタンスのDBを横断検索"""
        all_results = []
        for inst in instances:
            db_path = self._get_db_path(inst)
            if not db_path.exists():
                continue
            results = self._search_single_db(
                keywords, user_query, inst, limit, brain_conf, embedding_conf
            )
            for r in results:
                r["source_instance"] = inst
            all_results.extend(results)

        # スコアでリランキング → 上位 limit 件
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results[:limit]

    def _search_single_db(
        self, keywords, user_query, instance_name, limit, brain_conf,
        embedding_conf=None,
    ):
        """
        単一インスタンスDBに対するハイブリッド検索
        (キーワードフィルター + ベクトル類似度リランキング)
        """
        instance_db_path = self._get_db_path(instance_name)
        if not instance_db_path.exists():
            return []

        try:
            conn = sqlite3.connect(instance_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # ---------------------------------------------------------
            # 1. Broad Keyword Search (Filter)
            # ---------------------------------------------------------
            rows = []

            # 共通カラム定義: embedding -> embedding_blob
            select_cols = self._knowledge_select_cols(cursor)

            # Keyword Filter
            kw_conditions = []
            kw_params = []
            if keywords:
                for k in keywords:
                    kw_conditions.append("(title LIKE ? OR summary LIKE ?)")
                    kw_params.extend([f"%{k}%", f"%{k}%"])

            # --- Primary Search (Keyword Filter) ---
            if kw_conditions:
                where_sql = f"({' OR '.join(kw_conditions)})"

                # Fetch more candidates for reranking
                fetch_limit = brain_conf["fallback_fetch_limit"]
                query = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    WHERE {where_sql}
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query, kw_params)
                rows = cursor.fetchall()

            # --- Fallback Logic ---
            # キーワード検索結果が少なすぎる場合、直近のデータを無条件で取得して対象にする
            KEYWORD_HIT_THRESHOLD = brain_conf["keyword_hit_threshold"]

            if len(rows) < KEYWORD_HIT_THRESHOLD:
                # フォールバック: 直近 fallback_fetch_limit 件 (キーワード条件なし)
                print(
                    f"[Brain] Fallback triggered (Hits: {len(rows)}). "
                    f"Fetching recent {brain_conf['fallback_fetch_limit']} logs."
                )

                fetch_limit = brain_conf["fallback_fetch_limit"]
                query_fb = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query_fb)
                fallback_rows = cursor.fetchall()

                # 重複排除しながらマージ (IDをキーにする)
                existing_ids = {row["id"] for row in rows}
                for fb_row in fallback_rows:
                    if fb_row["id"] not in existing_ids:
                        rows.append(fb_row)

            conn.close()

            if not rows:
                return []

            # ---------------------------------------------------------
            # 2. Vector Reranking (BLOB / Float32)
            # ---------------------------------------------------------
            query_embedding = self.get_embedding(user_query, embedding_conf)
            if not query_embedding:
                # ベクトル生成失敗時はそのまま返す(上位limit件)。
                # embedding_blob(bytes) は候補 dict に残すと後段の JSON 化で
                # 落ちるため除外する
                return [
                    {k: v for k, v in dict(row).items() if k != "embedding_blob"}
                    for row in rows[:limit]
                ]

            scored_results = []

            # --- Scoring Modifiers ---
            decay_rate = brain_conf.get(
                "time_decay_rate", 0.005
            )  # 0.005 means half-life of ~138 days
            now = resolve_now()

            for row in rows:
                row_dict = dict(row)
                blob_data = row_dict.pop("embedding_blob")  # Remove from result dict
                is_archived = row_dict.get("is_archived") or 0

                score = 0.0
                if blob_data:
                    try:
                        # BLOB -> NumPy (float32)
                        db_emb = np.frombuffer(blob_data, dtype=np.float32)
                        score = float(
                            self._calculate_cosine_similarity(query_embedding, db_emb)
                        )

                        # Apply Time Decay (source_date=出来事日 優先)
                        basis_dt = _decay_basis_datetime(row_dict)
                        if basis_dt is not None:
                            days_diff = (now - basis_dt).days
                            if days_diff > 0:
                                decay_factor = math.exp(-decay_rate * days_diff)
                                score *= decay_factor

                        # Apply Archive Penalty
                        if is_archived:
                            score *= 0.5

                    except Exception:
                        pass

                row_dict["score"] = float(score)  # Ensure it is a float
                scored_results.append(row_dict)

            # Sort by score descending
            scored_results.sort(key=lambda x: x["score"], reverse=True)

            return scored_results[:limit]

        except Exception as e:
            print(f"[Brain] Search Error: {e}")
            return []
