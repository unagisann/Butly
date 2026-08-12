"""evidence=0 の問題を「検索経路のどこで落ちたか」に再分類する post-hoc 解析。

retrieval_hybrid_search_plan.ja.md §11-10「残った evidence=0 を『カード内にある /
カードに無い』に再分類する」を機械的に出せる形にしたもの。判定材料は scorer が
すでに scores.json へ書いている指標だけなので、run を再実行せず、過去の run にも
遡って使える（古い run は指標が無いので ``score`` の再実行が要る）。

分類は上流から順に見て最初に当たったものを採る。同時に複数の原因を持つ問題を
二重計上せず、「最初に落ちた場所」を1つだけ返す:

1. ``no_search``             検索そのものが走っていない（Gatekeeper が発火せず）
2. ``no_card``               根拠ターンを含むカードが1枚も無い（カード化で落ちた）
3. ``not_in_candidates``     候補 top20 にすら入らない（embedding / 検索クエリ）
4. ``rank_below_injection``  候補には居るが注入 top3 に来ない（ランキング / リランカー）
5. ``dropped_after_ranking`` top3 に居たのに注入されていない（注入 policy / 枠）

adversarial（category 5）は「No information」が正解なので、根拠が引けないこと
自体は失敗ではない。既定では分母から外し、参考値として別に数える。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# scorer が recall を測る k。注入枠が 3、候補プールが 20。
INJECTED_TOP_K = 3
CANDIDATE_TOP_K = 20

ADVERSARIAL_CATEGORY = 5

# 分類に必要で、古い scores.json には存在しないキー。
REQUIRED_KEYS = (
    "search_executed",
    "oracle_available",
    f"recall_at_{INJECTED_TOP_K}",
    f"recall_at_{CANDIDATE_TOP_K}",
)


@dataclass(frozen=True)
class Bucket:
    """evidence=0 の落ちた場所と、そこに効く打ち手。"""

    key: str
    label: str
    next_step: str


BUCKETS: tuple[Bucket, ...] = (
    Bucket(
        "no_search",
        "検索が走っていない",
        "Gatekeeper / need_intent",
    ),
    Bucket(
        "no_card",
        "根拠を含むカードが無い",
        "カード化（Sleeptime 抽出）",
    ),
    Bucket(
        "not_in_candidates",
        f"候補 top{CANDIDATE_TOP_K} に入らない",
        "embedding / 検索クエリ",
    ),
    Bucket(
        "rank_below_injection",
        f"候補には居るが top{INJECTED_TOP_K} 外",
        "ランキング / リランカー",
    ),
    Bucket(
        "dropped_after_ranking",
        f"top{INJECTED_TOP_K} に居たのに未注入",
        "注入 policy / 注入枠",
    ),
    Bucket(
        "unclassified",
        "判定材料が無い",
        "score を再実行（古い run は指標未収録）",
    ),
)

BUCKETS_BY_KEY: dict[str, Bucket] = {bucket.key: bucket for bucket in BUCKETS}


def classify(entry: Mapping[str, Any]) -> str:
    """1問を bucket key に分類する。evidence=0 の問題にのみ意味がある。

    指標そのものが scores.json に無い（古い run）場合は ``unclassified`` を返す。
    「値が False/None」と「キーが無い」を区別しないと、旧 run の全問が
    ``no_search`` に化ける。
    """
    if any(key not in entry for key in REQUIRED_KEYS):
        return "unclassified"
    if not entry["search_executed"]:
        return "no_search"
    if entry["oracle_available"] is False:
        return "no_card"
    recall_candidates = entry[f"recall_at_{CANDIDATE_TOP_K}"]
    recall_injected = entry[f"recall_at_{INJECTED_TOP_K}"]
    if recall_candidates is None or recall_injected is None:
        return "unclassified"
    if not recall_candidates:
        return "not_in_candidates"
    if not recall_injected:
        return "rank_below_injection"
    return "dropped_after_ranking"


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _official_scores(entries: Iterable[Mapping[str, Any]]) -> list[float]:
    return [
        float(entry["official_score"])
        for entry in entries
        if entry.get("official_score") is not None
    ]


def _by_category(entries: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("category"))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _is_adversarial(entry: Mapping[str, Any]) -> bool:
    return str(entry.get("category")) == str(ADVERSARIAL_CATEGORY)


def analyze_scores(
    scores: Mapping[str, Any],
    *,
    include_adversarial: bool = False,
) -> dict[str, Any]:
    """scores.json の内容から evidence 内訳を組み立てる。"""
    questions = list(scores.get("questions") or [])
    # evidence_coverage が None の問題は根拠ターンを持たないので、検索の
    # 成否を語れない。分母から外す。
    measured = [q for q in questions if q.get("evidence_coverage") is not None]
    adversarial = [q for q in measured if _is_adversarial(q)]
    target = measured if include_adversarial else [
        q for q in measured if not _is_adversarial(q)
    ]

    zero = [q for q in target if not q.get("evidence_coverage")]
    hit = [q for q in target if q.get("evidence_coverage")]

    grouped: dict[str, list[Mapping[str, Any]]] = {b.key: [] for b in BUCKETS}
    for entry in zero:
        grouped[classify(entry)].append(entry)

    buckets = []
    for bucket in BUCKETS:
        entries = grouped[bucket.key]
        buckets.append(
            {
                "key": bucket.key,
                "label": bucket.label,
                "next_step": bucket.next_step,
                "count": len(entries),
                "share": (len(entries) / len(zero)) if zero else None,
                "official_mean": _mean(_official_scores(entries)),
                "by_category": _by_category(entries),
                "question_ids": [
                    str(entry.get("question_id")) for entry in entries
                ],
            }
        )

    return {
        "run_id": scores.get("run_id"),
        "question_count": len(questions),
        "include_adversarial": include_adversarial,
        "measured_count": len(target),
        "evidence_zero_count": len(zero),
        "evidence_hit_count": len(hit),
        "adversarial_count": len(adversarial),
        "adversarial_evidence_zero_count": len(
            [q for q in adversarial if not q.get("evidence_coverage")]
        ),
        "official_overall": (scores.get("official") or {}).get("overall"),
        "official_mean_evidence_zero": _mean(_official_scores(zero)),
        "official_mean_evidence_hit": _mean(_official_scores(hit)),
        "buckets": buckets,
    }


def analyze_run(
    run_dir: Path,
    *,
    include_adversarial: bool = False,
) -> dict[str, Any]:
    """run ディレクトリの scores.json を読んで解析する。"""
    scores_path = Path(run_dir) / "scores.json"
    if not scores_path.is_file():
        raise FileNotFoundError(
            f"scores.json がありません: {scores_path}（先に score を実行）"
        )
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    analysis = analyze_scores(scores, include_adversarial=include_adversarial)
    analysis["run_dir"] = str(run_dir)
    return analysis


def _format_number(value: Optional[float], digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _format_share(value: Optional[float]) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def format_text(
    analysis: Mapping[str, Any],
    *,
    list_limit: int = 5,
    baseline: Optional[Mapping[str, Any]] = None,
) -> str:
    """人が読む内訳テキスト。baseline を渡すと件数を並べて差分を出す。"""
    lines: list[str] = []
    lines.append(f"# evidence 内訳 — {analysis.get('run_id')}")
    scope = (
        "全カテゴリ"
        if analysis.get("include_adversarial")
        else f"cat{ADVERSARIAL_CATEGORY} を除く"
    )
    lines.append(
        f"対象: 根拠ターンを持つ {analysis['measured_count']} 問"
        f"（{scope} / 全 {analysis['question_count']} 問）"
    )
    if analysis["measured_count"]:
        lines.append(
            f"evidence>0: {analysis['evidence_hit_count']}"
            f"（official {_format_number(analysis['official_mean_evidence_hit'])}）"
            f"  /  evidence=0: {analysis['evidence_zero_count']}"
            f"（official {_format_number(analysis['official_mean_evidence_zero'])}）"
        )
    if not analysis.get("include_adversarial") and analysis["adversarial_count"]:
        lines.append(
            f"参考: cat{ADVERSARIAL_CATEGORY} は {analysis['adversarial_count']} 問中"
            f" {analysis['adversarial_evidence_zero_count']} 問が evidence=0"
            "（No information が正解なので失敗とは限らない）"
        )
    lines.append("")

    baseline_counts: dict[str, int] = {}
    if baseline is not None:
        baseline_counts = {
            str(item["key"]): int(item["count"])
            for item in baseline.get("buckets", [])
        }
        lines.append(f"比較対象: {baseline.get('run_id')}")
        lines.append("")

    header = "| 落ちた場所 | 件数 |"
    divider = "| --- | ---: |"
    if baseline is not None:
        header += f" {baseline.get('run_id')} | 差 |"
        divider += " ---: | ---: |"
    header += " 割合 | official 平均 | 効く打ち手 |"
    divider += " ---: | ---: | --- |"
    lines.append(header)
    lines.append(divider)

    for bucket in analysis["buckets"]:
        if not bucket["count"] and bucket["key"] == "unclassified":
            continue
        row = f"| {bucket['label']} | {bucket['count']} |"
        if baseline is not None:
            before = baseline_counts.get(bucket["key"], 0)
            delta = bucket["count"] - before
            row += f" {before} | {delta:+d} |"
        row += (
            f" {_format_share(bucket['share'])}"
            f" | {_format_number(bucket['official_mean'])}"
            f" | {bucket['next_step']} |"
        )
        lines.append(row)

    unclassified = next(
        b for b in analysis["buckets"] if b["key"] == "unclassified"
    )
    if unclassified["count"]:
        lines.append("")
        lines.append(
            f"※ {unclassified['count']} 問は scores.json に検索指標が無く分類でき"
            "ません。`analyze-evidence` は score 再実行後の run で使えます。"
        )

    if list_limit:
        lines.append("")
        for bucket in analysis["buckets"]:
            if not bucket["count"]:
                continue
            shown = bucket["question_ids"][:list_limit]
            rest = bucket["count"] - len(shown)
            suffix = f" ほか{rest}問" if rest > 0 else ""
            categories = ", ".join(
                f"cat{key}×{count}"
                for key, count in bucket["by_category"].items()
            )
            lines.append(f"- {bucket['label']}（{categories}）")
            lines.append(f"  {', '.join(shown)}{suffix}")

    return "\n".join(lines)
