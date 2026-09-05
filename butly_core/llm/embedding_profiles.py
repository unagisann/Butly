"""
embedding_profiles.py
---------------------
Embedding モデルごとの「入力規約」をプロファイルとして持つレジストリ。

多くの検索用 embedding モデルは、クエリ側と文書側で別の prefix を要求する。
代表例:

  - nomic-embed-text v1/v1.5 : ``search_query: `` / ``search_document: ``
  - E5 系 (multilingual-e5-*) : ``query: `` / ``passage: ``
  - BGE (en v1.5)            : クエリ側のみ instruction を付ける
  - Qwen3-Embedding          : クエリ側のみ ``Instruct: ...\\nQuery: ``
  - gemini-embedding-2       : ``task: ... | query: `` / ``title: ... | text: ``

prefix を付け忘れると埋め込みが 1 つの円錐に潰れ、cosine の識別力が失われる
（実測: prefix 無しの nomic ではカード同士の cosine 平均 0.756 に対し、質問と
正解カードの cosine 平均 0.733 = 無関係なカード同士のほうが近い）。
モデルを差し替えるたびに手で prefix を書き換えるのは事故のもとなので、
モデル名から規約を引ける形にしてある。

解決順序 (``resolve_profile``):

  1. ``embedding.query_prefix`` / ``embedding.document_prefix`` の明示指定
     （未知モデル向けの脱出ハッチ。プロファイル定義より優先）
  2. ``embedding.profile`` の明示指定（プロファイル ID、``"plain"`` で無効化）
  3. ``embedding.profile`` 未指定 or ``"auto"`` → ``model_name`` から推定
  4. どれにも当たらなければ ``plain``（prefix 無し）

``dim`` は保存済みベクトルとの突き合わせ（embedding_check）に使う。
config.py は import しない（循環防止）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

Kind = str  # "query" | "document"

QUERY: Kind = "query"
DOCUMENT: Kind = "document"


@dataclass(frozen=True)
class EmbeddingProfile:
    """1 つの embedding モデル族の入力規約。"""

    id: str
    query_prefix: str = ""
    document_prefix: str = ""
    dim: Optional[int] = None
    # model_name に対する部分一致パターン（小文字で比較）
    match: tuple[str, ...] = ()
    notes: str = ""

    def prefix_for(self, kind: Kind) -> str:
        return self.query_prefix if kind == QUERY else self.document_prefix

    def apply(self, text: str, kind: Kind) -> str:
        """text に kind 側の prefix を付けて返す。"""
        prefix = self.prefix_for(kind)
        if not prefix or not text:
            return text
        if text.startswith(prefix):
            # 二重付与を防ぐ（migrate の再実行や呼び出し側の二重適用対策）
            return text
        return f"{prefix}{text}"


PLAIN = EmbeddingProfile(
    id="plain",
    notes="prefix 無し。OpenAI text-embedding-3-* や規約不明のモデル向け。",
)

BUILTIN_EMBEDDING_PROFILES: tuple[EmbeddingProfile, ...] = (
    PLAIN,
    EmbeddingProfile(
        id="nomic",
        query_prefix="search_query: ",
        document_prefix="search_document: ",
        dim=768,
        match=("nomic-embed",),
        notes="nomic-embed-text v1/v1.5。prefix 必須。",
    ),
    EmbeddingProfile(
        id="e5",
        query_prefix="query: ",
        document_prefix="passage: ",
        match=("e5-", "-e5", "multilingual-e5"),
        notes="intfloat E5 系（multilingual-e5-* を含む）。prefix 必須。",
    ),
    EmbeddingProfile(
        id="bge-instruct",
        query_prefix=(
            "Represent this sentence for searching relevant passages: "
        ),
        match=("bge-large-en", "bge-base-en", "bge-small-en"),
        notes="BGE 英語 v1.5 系。クエリ側のみ instruction を付ける。",
    ),
    EmbeddingProfile(
        id="bge-m3",
        dim=1024,
        match=("bge-m3",),
        notes="BGE-M3 は多言語で prefix 不要。",
    ),
    EmbeddingProfile(
        id="qwen3-embedding",
        query_prefix=(
            "Instruct: Given a search query, retrieve relevant passages "
            "that answer the query\nQuery: "
        ),
        match=("qwen3-embedding",),
        notes="Qwen3-Embedding。クエリ側のみ instruction を付ける。",
    ),
    EmbeddingProfile(
        id="openai",
        dim=1536,
        match=("text-embedding-3-small", "text-embedding-ada-002"),
        notes="OpenAI text-embedding-3-small / ada-002。prefix 不要。",
    ),
    EmbeddingProfile(
        id="openai-large",
        dim=3072,
        match=("text-embedding-3-large",),
        notes="OpenAI text-embedding-3-large。prefix 不要。",
    ),
    EmbeddingProfile(
        id="gemini",
        dim=3072,
        match=("gemini-embedding",),
        notes=(
            "Gemini embedding (001 系)。prefix ではなく task_type パラメータで "
            "クエリ/文書を区別する仕様のため prefix は付けない。"
        ),
    ),
    EmbeddingProfile(
        id="gemini-embedding-2",
        dim=3072,
        query_prefix="task: question answering | query: ",
        document_prefix="title: none | text: ",
        match=("gemini-embedding-2",),
        notes=(
            "gemini-embedding-2。001 と違い task_type パラメータを受け付けず、"
            "タスク指示をテキスト側に埋める仕様 "
            "(https://ai.google.dev/gemini-api/docs/embeddings)。"
            "クエリ側の task 名は用途で差し替え可 "
            "(search result / question answering / fact checking / "
            "code retrieval)。文書側は title を持たないので "
            "公式の推奨どおり 'title: none' を使う。"
        ),
    ),
    EmbeddingProfile(
        id="gemini-004",
        dim=768,
        match=("text-embedding-004",),
        notes="Gemini text-embedding-004。prefix 不要。",
    ),
    EmbeddingProfile(
        id="mxbai",
        dim=1024,
        match=("mxbai-embed",),
        notes="mxbai-embed-large。prefix 不要。",
    ),
)

_BY_ID: dict[str, EmbeddingProfile] = {p.id: p for p in BUILTIN_EMBEDDING_PROFILES}


def list_profiles() -> tuple[EmbeddingProfile, ...]:
    """登録済みプロファイル一覧（UI の選択肢用）。"""
    return tuple(_BY_ID.values())


def get_profile(profile_id: str) -> Optional[EmbeddingProfile]:
    if not profile_id:
        return None
    return _BY_ID.get(profile_id.strip().lower())


def register_profile(profile: EmbeddingProfile) -> None:
    """プロファイルを追加/上書きする（プラグインやテスト用）。"""
    _BY_ID[profile.id] = profile


def _normalize_model_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    n = name.strip()
    if n.startswith("models/"):
        n = n[len("models/") :]
    # "org/model" 形式（HuggingFace や NanoGPT 等のルーター）は末尾を見る
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    return n.lower()


def infer_profile(model_name: Any) -> EmbeddingProfile:
    """model_name からプロファイルを推定する。当たらなければ plain。"""
    n = _normalize_model_name(model_name)
    if not n:
        return PLAIN
    best: Optional[EmbeddingProfile] = None
    best_len = -1
    for profile in _BY_ID.values():
        for pattern in profile.match:
            if pattern in n and len(pattern) > best_len:
                best = profile
                best_len = len(pattern)
    return best or PLAIN


def resolve_profile(embedding_conf: Optional[dict]) -> EmbeddingProfile:
    """embedding 設定から実際に使うプロファイルを決める。

    モジュール docstring の解決順序に従う。戻り値は必ず非 None。
    """
    conf = embedding_conf or {}
    model_name = conf.get("model_name", "")

    explicit_q = conf.get("query_prefix")
    explicit_d = conf.get("document_prefix")
    if explicit_q is not None or explicit_d is not None:
        base = _resolve_declared(conf) or infer_profile(model_name)
        return EmbeddingProfile(
            id=f"{base.id}+custom",
            query_prefix=(
                explicit_q if explicit_q is not None else base.query_prefix
            ),
            document_prefix=(
                explicit_d if explicit_d is not None else base.document_prefix
            ),
            dim=base.dim,
            notes="config で prefix を明示指定",
        )

    declared = _resolve_declared(conf)
    if declared is not None:
        return declared

    return infer_profile(model_name)


def _resolve_declared(conf: dict) -> Optional[EmbeddingProfile]:
    """``profile`` キーの明示指定を解決する（"auto"/未指定は None）。"""
    declared = conf.get("profile")
    if not isinstance(declared, str) or not declared.strip():
        return None
    key = declared.strip().lower()
    if key == "auto":
        return None
    found = get_profile(key)
    if found is None:
        print(
            f"[Embedding] WARNING: unknown embedding profile '{declared}'. "
            f"Falling back to auto-detection. "
            f"Known: {', '.join(sorted(_BY_ID))}"
        )
        return None
    return found


def apply_prefix(
    text: str, embedding_conf: Optional[dict], kind: Kind = QUERY
) -> str:
    """embedding_conf に対応する prefix を text に付けて返す。"""
    return resolve_profile(embedding_conf).apply(text, kind)


def fingerprint(embedding_conf: Optional[dict]) -> dict:
    """保存済みベクトルとの突き合わせ用の指紋。

    モデル名とプロファイル ID の組。どちらかが変われば、既存の
    ``embedding_blob`` は新しいクエリベクトルと比較できない。
    """
    conf = embedding_conf or {}
    profile = resolve_profile(conf)
    return {
        "model_name": _normalize_model_name(conf.get("model_name", "")),
        "profile": profile.id,
        "dim": profile.dim,
    }


def known_dims() -> dict[str, int]:
    """``model_name`` → 既知の次元数（embedding_check 用）。"""
    dims: dict[str, int] = {}
    for profile in _BY_ID.values():
        if profile.dim is None:
            continue
        for pattern in profile.match:
            dims[pattern] = profile.dim
    return dims


def describe(embedding_conf: Optional[dict]) -> str:
    """ログ表示用の 1 行サマリ。"""
    conf = embedding_conf or {}
    profile = resolve_profile(conf)
    q = profile.query_prefix or "(none)"
    d = profile.document_prefix or "(none)"
    return (
        f"model={conf.get('model_name', '?')} profile={profile.id} "
        f"query_prefix={q!r} document_prefix={d!r}"
    )


__all__ = [
    "DOCUMENT",
    "QUERY",
    "BUILTIN_EMBEDDING_PROFILES",
    "EmbeddingProfile",
    "apply_prefix",
    "describe",
    "fingerprint",
    "get_profile",
    "infer_profile",
    "known_dims",
    "list_profiles",
    "register_profile",
    "resolve_profile",
]
