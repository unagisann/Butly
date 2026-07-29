"""
hybrid_search.py
----------------
FTS5(trigram) の BM25 とベクトル検索を RRF で融合するための部品。

置き場所の理由: 索引の作成/同期は ``ButlyDatabase`` が、検索は ``ButlyBrain`` が
呼ぶ。どちらからも import できる中立な位置に置く（相互 import を作らない）。

trigram tokenizer を選んだのは形態素解析器なしで日英とも引けるからだが、
tokenizer の素の挙動は検索としては粗い。実測（SQLite 3.46.1）:

  - ``"cat"`` が ``catalogs`` / ``communication`` に当たる（語境界を見ない）
  - ``"陶芸"`` は 0 件（2文字では trigram を作れない）
  - 会話の主役名のような高DF語は、ほぼ全カードに当たる

そのため BM25 候補は「FTS でヒット → 語境界の再検証 → df ゲート → 候補数上限」の
3段補正を通してから順位付けする（計画書 §3.2）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

FTS_TABLE = "knowledge_cards_fts"
FTS_TOKENIZER = "trigram"
FTS_TEXT_COLUMNS = ("title", "tags", "summary", "episode")
# 索引の作り方（列構成・tokenizer・トリガ）を変えたら上げる。
# 不一致を検出した DB は単一トランザクションで作り直す。
FTS_SCHEMA_VERSION = 1

# 3文字未満は trigram を作れないため、CJK はここから漏れる語を LIKE 補助で拾う。
MIN_TERM_LEN = 3
SHORT_CJK_TERM_LEN = 2

DEFAULT_MAX_TERMS = 32
DEFAULT_BM25_WEIGHTS = {"title": 5.0, "tags": 3.0, "summary": 2.0, "episode": 1.0}

# 「検索語として意味を持たない」欧文語。CJK 側は助詞由来のノイズが多すぎて
# 列挙にならないので、df ゲート（高DF語の除外）に任せる。
STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "were", "with", "that", "this", "there",
        "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
        "did", "does", "done", "doing", "have", "has", "had", "having",
        "you", "your", "yours", "she", "her", "hers", "him", "his", "they", "them",
        "their", "theirs", "our", "ours", "its", "from", "into", "onto", "about",
        "any", "all", "some", "than", "then", "them", "these", "those", "such",
        "can", "could", "would", "should", "will", "shall", "may", "might", "must",
        "not", "but", "out", "off", "over", "under", "again", "also", "just",
        "tell", "told", "say", "said", "know", "knew", "let", "get", "got",
        "please", "thanks", "thank",
    }
)

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")
# ひらがな・カタカナ・漢字・ハングル。語境界が無いので連続する塊として切り出す。
_CJK_RUN_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+"
)


@dataclass(frozen=True)
class QuerySpec:
    """決定論的に組み立てた検索語。LLM は介在しない。"""

    match_expr: str
    terms: tuple[str, ...] = ()
    short_terms: tuple[str, ...] = ()
    ascii_terms: frozenset = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        return not self.terms and not self.short_terms


def build_fts_query(text: str, max_terms: int = DEFAULT_MAX_TERMS) -> QuerySpec:
    """質問文から FTS5 MATCH 式と検証用の語リストを作る。

    - NFKC 正規化 + 小文字化
    - 欧文: 3文字以上の英数字語（stopword 除去）
    - CJK: 連続する文字列から3文字 shingle。2文字語は ``short_terms`` へ回して
      LIKE 補助候補で拾う（trigram では引けないため）
    - ユーザー入力を MATCH の構文として連結しない。各語は quote し、``"`` は
      二重化する。SQL 値自体は parameter binding で渡す
    """
    if not text:
        return QuerySpec(match_expr="")

    normalized = unicodedata.normalize("NFKC", text).lower()

    terms: list[str] = []
    short_terms: list[str] = []
    ascii_terms: set = set()
    seen: set = set()

    def _add(term: str, bucket: list[str]) -> None:
        if term in seen:
            return
        seen.add(term)
        bucket.append(term)

    for token in _ASCII_TOKEN_RE.findall(normalized):
        if len(token) < MIN_TERM_LEN or token in STOPWORDS:
            continue
        _add(token, terms)
        ascii_terms.add(token)

    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == SHORT_CJK_TERM_LEN:
            _add(run, short_terms)
            continue
        for i in range(len(run) - MIN_TERM_LEN + 1):
            _add(run[i:i + MIN_TERM_LEN], terms)

    terms = terms[:max_terms]
    short_terms = short_terms[:max_terms]
    match_expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
    return QuerySpec(
        match_expr=match_expr,
        terms=tuple(terms),
        short_terms=tuple(short_terms),
        ascii_terms=frozenset(ascii_terms & set(terms)),
    )


def term_matches(term: str, text: str, *, is_ascii: bool) -> bool:
    """語がテキストに「本当に」一致しているか判定する。

    trigram は部分文字列一致なので ``cat`` が ``catalogs`` に当たる。欧文語は
    語頭一致 + 語尾3文字までに絞ることで ``pet``→``pets`` は残し
    ``carpet`` / ``communication`` を落とす。CJK は語境界が無いのでそのまま。
    """
    if not text:
        return False
    if not is_ascii:
        return term in text
    pattern = rf"(?<![a-z0-9]){re.escape(term)}[a-z]{{0,3}}(?![a-z0-9])"
    return re.search(pattern, text) is not None


# --------------------------------------------------------------------------
# 索引の作成・同期
# --------------------------------------------------------------------------


def fts5_trigram_available(conn: sqlite3.Connection) -> bool:
    """この SQLite で FTS5 + trigram が使えるか（配布環境の SQLite 差対策）。"""
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x, "
            f"tokenize='{FTS_TOKENIZER}')"
        )
        conn.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.Error as e:
        logger.warning("FTS5/trigram unavailable: %s", e)
        return False


def fts_index_ready(conn: sqlite3.Connection) -> bool:
    """検索側から使える索引が存在するか（作らない・軽い確認のみ）。"""
    try:
        row = conn.execute(
            "SELECT schema_version FROM fts_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == FTS_SCHEMA_VERSION


def ensure_fts_index(conn: sqlite3.Connection) -> dict:
    """FTS 索引・トリガ・backfill を冪等に用意する。

    「FTS が空か」だけでは部分欠損や tokenizer 変更を検出できないので、
    ``fts_meta`` の schema version と本体/FTS の件数の両方を見て、
    不一致のときだけ単一トランザクションで作り直す。

    Returns: 診断 dict（``available`` / ``rebuilt`` / ``card_count`` / ``reason``）。
    """
    diag = {
        "available": False,
        "rebuilt": False,
        "card_count": 0,
        "reason": "",
    }
    if not fts5_trigram_available(conn):
        diag["reason"] = "fts5_trigram_unavailable"
        return diag

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fts_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER,
                tokenizer TEXT,
                card_count INTEGER,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
                card_id UNINDEXED,
                {", ".join(FTS_TEXT_COLUMNS)},
                tokenize='{FTS_TOKENIZER}'
            )
            """
        )
        _create_triggers(conn)

        card_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_cards"
        ).fetchone()[0]
        fts_count = conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
        meta = conn.execute(
            "SELECT schema_version, tokenizer FROM fts_meta WHERE id = 1"
        ).fetchone()

        diag["card_count"] = card_count
        needs_rebuild = (
            meta is None
            or meta[0] != FTS_SCHEMA_VERSION
            or meta[1] != FTS_TOKENIZER
            or fts_count != card_count
        )
        if needs_rebuild:
            _rebuild_index(conn, card_count)
            diag["rebuilt"] = True
            diag["reason"] = (
                "schema_version" if meta is None or meta[0] != FTS_SCHEMA_VERSION
                else "count_mismatch"
            )
        diag["available"] = True
        return diag
    except sqlite3.Error as e:
        logger.warning("FTS index setup failed: %s", e)
        diag["reason"] = f"error:{e}"
        return diag


def _create_triggers(conn: sqlite3.Connection) -> None:
    """本体テーブルへの書き手（Sleeptime / Stage 3 / migrate）を問わず同期させる。

    UPDATE は本文列が変わったときだけ張る。embedding だけを書き換える
    migration で無駄な再索引を走らせないため。
    """
    cols = ", ".join(FTS_TEXT_COLUMNS)
    new_cols = ", ".join(f"new.{c}" for c in FTS_TEXT_COLUMNS)
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS knowledge_cards_fts_ai
        AFTER INSERT ON knowledge_cards BEGIN
            INSERT INTO {FTS_TABLE}(card_id, {cols})
            VALUES (new.id, {new_cols});
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS knowledge_cards_fts_ad
        AFTER DELETE ON knowledge_cards BEGIN
            DELETE FROM {FTS_TABLE} WHERE card_id = old.id;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS knowledge_cards_fts_au
        AFTER UPDATE OF id, {cols} ON knowledge_cards BEGIN
            DELETE FROM {FTS_TABLE} WHERE card_id = old.id;
            INSERT INTO {FTS_TABLE}(card_id, {cols})
            VALUES (new.id, {new_cols});
        END
        """
    )


def _rebuild_index(conn: sqlite3.Connection, card_count: int) -> None:
    cols = ", ".join(FTS_TEXT_COLUMNS)
    conn.execute(f"DELETE FROM {FTS_TABLE}")
    conn.execute(
        f"INSERT INTO {FTS_TABLE}(card_id, {cols}) "
        f"SELECT id, {cols} FROM knowledge_cards"
    )
    conn.execute(
        """
        INSERT INTO fts_meta (id, schema_version, tokenizer, card_count, updated_at)
        VALUES (1, ?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            schema_version = excluded.schema_version,
            tokenizer = excluded.tokenizer,
            card_count = excluded.card_count,
            updated_at = excluded.updated_at
        """,
        (FTS_SCHEMA_VERSION, FTS_TOKENIZER, card_count),
    )


# --------------------------------------------------------------------------
# BM25 候補
# --------------------------------------------------------------------------


def bm25_candidates(
    conn: sqlite3.Connection,
    spec: QuerySpec,
    *,
    columns: Iterable[str],
    limit: int,
    weights: Optional[dict] = None,
    max_df_ratio: float = 0.5,
    min_weak_df: int = 5,
    scan_limit: int = 500,
) -> dict:
    """BM25 候補を返す（語境界検証 → df ゲート → archive 判定 → 上位 limit）。

    Returns:
        {"results": [...], "diagnostics": {...}}
        results は BM25 の良い順。``bm25_rank`` は 1 始まりの順位、
        ``bm25_score`` は SQLite の bm25()（小さいほど良い）。
    """
    diag = {
        "terms": list(spec.terms),
        "short_terms": list(spec.short_terms),
        "scanned": 0,
        "verified": 0,
        "weak_terms": [],
        "short_term_hits": 0,
        "archived_fallback": False,
        "truncated": False,
    }
    if spec.is_empty():
        return {"results": [], "diagnostics": diag}

    total_cards = conn.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone()[0]
    if not total_cards:
        return {"results": [], "diagnostics": diag}

    # 語境界検証と archive 判定に要る列は、呼び出し側の指定に関わらず必ず引く
    select_cols = _select_columns(columns)
    rows: list[dict] = []
    if spec.match_expr:
        rows = _scan_fts(conn, spec, select_cols, weights, scan_limit)
    short_rows = (
        _scan_short_terms(conn, spec, select_cols, scan_limit)
        if spec.short_terms
        else []
    )

    scanned = rows + [r for r in short_rows if r["id"] not in {x["id"] for x in rows}]
    diag["scanned"] = len(scanned)
    diag["truncated"] = len(rows) >= scan_limit

    verified = []
    for row in scanned:
        matched = _verified_terms(row, spec)
        if matched:
            row["matched_terms"] = matched
            verified.append(row)
    diag["verified"] = len(verified)

    weak = _weak_terms(verified, total_cards, max_df_ratio, min_weak_df)
    diag["weak_terms"] = sorted(weak)
    kept = [r for r in verified if set(r["matched_terms"]) - weak]
    for row in kept:
        row["matched_terms"] = sorted(set(row["matched_terms"]) - weak)

    active = [r for r in kept if not r.get("is_archived")]
    if active:
        kept = active
    elif kept:
        # 字面一致は archived にも容易に当たるので、active が皆無のときだけ使う
        diag["archived_fallback"] = True

    kept.sort(key=bm25_sort_key)
    kept = kept[:limit]
    diag["short_term_hits"] = sum(
        1 for r in kept if r.get("bm25_score") is None
    )
    for rank, row in enumerate(kept, start=1):
        row["bm25_rank"] = rank
    return {"results": kept, "diagnostics": diag}


def bm25_sort_key(row: dict) -> tuple:
    """BM25 ヒットを先に、LIKE 補助候補（スコア無し）を一致語数順で後ろに。"""
    score = row.get("bm25_score")
    if score is None:
        return (1, 0.0, -len(row.get("matched_terms") or ()), str(row.get("id")))
    return (0, float(score), 0, str(row.get("id")))


def _scan_fts(
    conn: sqlite3.Connection,
    spec: QuerySpec,
    select_cols: tuple,
    weights: Optional[dict],
    scan_limit: int,
) -> list[dict]:
    w = {**DEFAULT_BM25_WEIGHTS, **(weights or {})}
    weight_sql = ", ".join(
        ["0.0"] + [str(float(w.get(c, 1.0))) for c in FTS_TEXT_COLUMNS]
    )
    query = f"""
        SELECT {_prefixed(select_cols)}, m.bm25_score
        FROM (
            SELECT card_id, bm25({FTS_TABLE}, {weight_sql}) AS bm25_score
            FROM {FTS_TABLE}
            WHERE {FTS_TABLE} MATCH ?
            ORDER BY bm25_score
            LIMIT ?
        ) m
        JOIN knowledge_cards k ON k.id = m.card_id
        ORDER BY m.bm25_score
    """
    try:
        cursor = conn.execute(query, (spec.match_expr, scan_limit))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.warning("BM25 scan failed: %s", e)
        return []


def _scan_short_terms(
    conn: sqlite3.Connection,
    spec: QuerySpec,
    select_cols: tuple,
    scan_limit: int,
) -> list[dict]:
    """2文字 CJK 語の LIKE 補助候補（trigram では引けないため）。"""
    conditions = []
    params: list[Any] = []
    for term in spec.short_terms:
        conditions.append(
            "(" + " OR ".join(f"k.{c} LIKE ?" for c in FTS_TEXT_COLUMNS) + ")"
        )
        params.extend([f"%{term}%"] * len(FTS_TEXT_COLUMNS))
    query = f"""
        SELECT {_prefixed(select_cols)}, NULL AS bm25_score
        FROM knowledge_cards k
        WHERE {" OR ".join(conditions)}
        LIMIT ?
    """
    try:
        cursor = conn.execute(query, (*params, scan_limit))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.warning("Short-term scan failed: %s", e)
        return []


def _select_columns(columns: Iterable[str]) -> tuple:
    """呼び出し側が欲しい列 + 検証に必須の列（重複なし・順序保持）。"""
    required = ("id", "is_archived", *FTS_TEXT_COLUMNS)
    ordered: list[str] = []
    for col in (*columns, *required):
        name = col.strip()
        if name and name != "embedding_blob" and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def _prefixed(select_cols: tuple) -> str:
    return ", ".join(f"k.{c}" for c in select_cols)


def _row_text(row: dict) -> str:
    return "\n".join(
        str(row.get(c) or "") for c in FTS_TEXT_COLUMNS
    ).lower()


def _verified_terms(row: dict, spec: QuerySpec) -> list[str]:
    text = _row_text(row)
    matched = [
        t
        for t in spec.terms
        if term_matches(t, text, is_ascii=t in spec.ascii_terms)
    ]
    matched += [t for t in spec.short_terms if t in text]
    return matched


def _weak_terms(
    rows: Iterable[dict],
    total_cards: int,
    max_df_ratio: float,
    min_weak_df: int,
) -> set:
    """高DF語（会話の主役名・助詞由来 shingle 等）を弱い語として集める。

    候補集合 = 「1語以上が一致したカード」なので、この集合内の df は
    コーパス全体の df と一致する（スキャン打ち切り時のみ下限）。

    ``min_weak_df`` は件数の少ない DB を守るための床。カードが3枚しかない
    段階では「2枚に出る語」も比率では 0.67 になるが、それはノイズではない。
    """
    df: dict = {}
    for row in rows:
        for term in row["matched_terms"]:
            df[term] = df.get(term, 0) + 1
    return {
        t
        for t, count in df.items()
        if count >= min_weak_df and count / max(total_cards, 1) > max_df_ratio
    }


# --------------------------------------------------------------------------
# RRF 融合
# --------------------------------------------------------------------------


def rrf_fuse(
    vector_results: list[dict],
    bm25_results: list[dict],
    *,
    k: int = 60,
    limit: int = 3,
    key: Optional[Any] = None,
) -> list[dict]:
    """順位のみを使って2つのランキングを融合する（Reciprocal Rank Fusion）。

    返す dict の ``score`` は **RRF スコア**。下流は複数箇所で ``score`` 降順に
    並べ直すため、cosine を ``score`` に残すと融合が黙って無効化される
    （計画書 §3.2 の実装契約）。cosine は ``vector_score`` / ``raw_score`` に残す。
    """
    key_fn = key or (lambda row: (row.get("source_instance"), row.get("id")))
    fused: dict = {}

    def _merge(rows: list[dict], rank_field: str, source: str) -> None:
        for rank, row in enumerate(rows, start=1):
            row_key = key_fn(row)
            entry = fused.get(row_key)
            if entry is None:
                entry = dict(row)
                entry["rrf_score"] = 0.0
                entry["retrieval_source"] = source
                fused[row_key] = entry
            else:
                for field_name, value in row.items():
                    if entry.get(field_name) is None:
                        entry[field_name] = value
                if entry["retrieval_source"] != source:
                    entry["retrieval_source"] = "both"
            entry[rank_field] = rank
            entry["rrf_score"] += 1.0 / (k + rank)

    _merge(vector_results, "vector_rank", "vector")
    _merge(bm25_results, "bm25_rank", "bm25")

    results = list(fused.values())
    for row in results:
        row["score"] = row["rrf_score"]
        row.setdefault("vector_rank", None)
        row.setdefault("bm25_rank", None)
    results.sort(
        key=lambda r: (
            -r["rrf_score"],
            r.get("vector_rank") or 10**6,
            r.get("bm25_rank") or 10**6,
            str(r.get("id")),
        )
    )
    return results[:limit]
