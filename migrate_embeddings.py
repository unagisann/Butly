"""
migrate_embeddings.py
---------------------
埋め込みベクトルマイグレーションユーティリティ。

プロバイダー切り替え時（例: Gemini → OpenAI）に既存の embedding_blob を
新しいプロバイダーで再生成する。

使い方:
    python migrate_embeddings.py [--instance INSTANCE_NAME] [--batch-size N] [--dry-run]
"""

import argparse
import sqlite3
import struct
import sys
import time
from pathlib import Path

import numpy as np

# プロジェクトルートをパスに追加
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.core.card_content import build_embed_text
from butly_core.core.embedding_check import record_embedding_meta
from butly_core.llm.embedding_profiles import DOCUMENT
from butly_core.llm.embedding_profiles import apply_prefix as apply_embedding_prefix
from butly_core.llm.embedding_profiles import describe as describe_embedding
from butly_core.llm.factory import ProviderFactory


RETRYABLE_MARKERS = ("429", "RESOURCE_EXHAUSTED", "503", "500", "UNAVAILABLE")


def embed_with_retry(provider, content, emb_conf, retries: int = 5, base_delay: int = 5):
    """レート制限 (429) を指数バックオフで再試行する。

    再試行しても駄目なら例外を送出する。ここで None を返すと、その 1 枚だけ
    旧空間のベクトルが残ったまま「移行済み」の見た目になる。
    """
    last_error = None
    for attempt in range(retries):
        try:
            return provider.embed(content, config=emb_conf)
        except Exception as e:  # SDK の例外階層が provider ごとに違う
            last_error = e
            if not any(m in str(e) for m in RETRYABLE_MARKERS):
                raise
            wait = base_delay * (2 ** attempt)
            print(
                f"  [Retry] {type(e).__name__}: {e} — {wait}s 待機 "
                f"({attempt + 1}/{retries})"
            )
            time.sleep(wait)
    raise RuntimeError(f"リトライ上限に到達: {last_error}")


def get_db_path(instance_name: str) -> Path:
    db_name = SYSTEM_CONFIG["paths"]["db_name"]
    return BASE_DIR / "butly_core" / "instances" / instance_name / db_name


def migrate_instance(instance_name: str, batch_size: int = 10, dry_run: bool = False):
    """
    指定インスタンスの全 knowledge_cards の embedding_blob を再生成する。

    Parameters
    ----------
    instance_name : str
        対象インスタンス名（例: "00_master"）
    batch_size : int
        一度に処理するレコード数（API レート制限対策）
    dry_run : bool
        True の場合、実際のDB更新は行わず処理対象の数のみ表示する
    """
    db_path = get_db_path(instance_name)
    if not db_path.exists():
        print(f"[Migration] DB not found: {db_path}")
        return

    # connection + model_name の dict 全体を渡す。model_name 文字列だけだと
    # ユーザー定義 connection を解決できない（sleeptime 側は d9d717c で修正済み）。
    emb_conf = AI_CONFIG["embedding"]
    model_name = emb_conf.get("model_name")
    provider = ProviderFactory.create(emb_conf)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 対象レコード取得
    cursor.execute(
        "SELECT id, title, tags, summary FROM knowledge_cards ORDER BY created_at DESC"
    )
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        print(f"[Migration] {instance_name}: レコードなし")
        conn.close()
        return

    print(
        f"[Migration] {instance_name}: {total} 件の埋め込みを再生成します "
        f"({describe_embedding(emb_conf)})"
    )

    if dry_run:
        print("[Migration] Dry-run モード — DB更新はスキップします")
        conn.close()
        return

    updated = 0
    failed = 0

    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        for row in batch:
            card_id = row["id"]
            # 文面は Sleeptime の保存経路と同じ helper で作る（分岐すると
            # 保存済みベクトルと再生成ベクトルが別空間になる）。
            content = build_embed_text(row["title"], row["tags"], row["summary"])
            # 書き込み側は文書 prefix。Sleeptime の保存経路と同じ規約を通す。
            content = apply_embedding_prefix(content, emb_conf, DOCUMENT)

            try:
                embedding = embed_with_retry(provider, content, emb_conf)
                if not embedding:
                    print(f"  [{card_id}] embed() returned no vector — skipping")
                    failed += 1
                    continue

                blob = np.array(embedding, dtype=np.float32).tobytes()
                cursor.execute(
                    "UPDATE knowledge_cards SET embedding_blob = ? WHERE id = ?",
                    (blob, card_id),
                )
                updated += 1
            except Exception as e:
                print(f"  [{card_id}] Error: {e}")
                failed += 1

        conn.commit()
        processed = min(i + batch_size, total)
        print(f"  Progress: {processed}/{total} ({updated} updated, {failed} failed)")

        # レート制限対策: バッチ間に短い待機
        if i + batch_size < total:
            time.sleep(1)

    conn.close()
    if updated and not failed:
        record_embedding_meta(db_path, emb_conf)
    elif failed:
        # 1 枚でも失敗が残っていると DB は新旧ベクトルの混在。ここで
        # embedding_meta を新設定で上書きすると、起動時の profile 不一致
        # 警告まで消えて混在に気づけなくなる。
        print(
            f"[Migration] WARNING: {failed} 件が未移行のため embedding_meta は "
            f"更新しない。失敗分を解消してから再実行すること。"
        )
    print(f"[Migration] 完了: {updated} updated, {failed} failed (total {total})")


def main():
    parser = argparse.ArgumentParser(description="埋め込みベクトル再生成ユーティリティ")
    parser.add_argument("--instance", default="00_master", help="対象インスタンス名")
    parser.add_argument("--all", action="store_true", help="全インスタンスを処理")
    parser.add_argument("--batch-size", type=int, default=10, help="バッチサイズ")
    parser.add_argument("--dry-run", action="store_true", help="DB更新せず件数のみ表示")
    args = parser.parse_args()

    if args.all:
        instances_dir = BASE_DIR / "butly_core" / "instances"
        for d in sorted(instances_dir.iterdir()):
            if d.is_dir():
                db_path = get_db_path(d.name)
                if db_path.exists():
                    migrate_instance(d.name, args.batch_size, args.dry_run)
    else:
        migrate_instance(args.instance, args.batch_size, args.dry_run)


if __name__ == "__main__":
    main()
