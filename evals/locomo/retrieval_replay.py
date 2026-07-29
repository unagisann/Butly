"""
retrieval_replay.py
-------------------
既存 run の記憶に対して**検索だけ**を差し替えて比較する offline replay。

LLM の回答生成を伴わないので、`rerun-qa` を回す前に「そもそも根拠カードへ
届くようになったのか」だけを安く測れる（検索改修計画 §8）。同じカード・同じ
質問に対して `search_mode` を変え、`retrieval_recall_at_k` を並べる。

    # BM25 のみ（embedding 呼び出しなし＝APIキー不要）
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes bm25

    # ベクトル込みの本比較（質問1件につき embedding 1回を呼ぶ）
    python -m evals.locomo.retrieval_replay --run ./eval_runs/runs/qwen3_14b_web_v26 \
        --modes vector hybrid --profile ./eval_runs/profiles/<id>.yaml

出力は stdout のサマリと、`--out` 指定時に JSON。検索は元 run の DB を一時
ディレクトリへ複製してから走らせるので、**元 run は一切変更しない**（R7）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from butly_core.core import hybrid_search as hs
from evals.locomo import scorer as S

RECALL_KS = (1, 3, 20)
BM25_COLUMNS = ["id", "title", "summary", "episode", "is_archived"]


def load_questions(run_dir: Path) -> list[dict]:
    """qa_results.jsonl から質問・evidence・instance を取り出す。"""
    path = run_dir / "results" / "qa_results.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"qa_results.jsonl not found: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def instance_db(base_dir: Path, instance_name: str) -> Optional[Path]:
    instance_dir = base_dir / "butly_core" / "instances" / instance_name
    if not instance_dir.is_dir():
        return None
    return next(iter(sorted(instance_dir.glob("*.db"))), None)


def mirror_workspace(run_dir: Path, dest: Path) -> Path:
    """検索対象の DB だけを一時 workspace へ複製する。

    replay は FTS 索引を作る（＝書き込む）ので、元 run の workspace は触らない。
    複製するのは `*.db`（+ WAL/SHM）だけで、カードと embedding はそのまま付いてくる。
    """
    source = run_dir / "workspace" / "butly_core" / "instances"
    for instance_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        target = dest / "butly_core" / "instances" / instance_dir.name
        target.mkdir(parents=True, exist_ok=True)
        for db_file in instance_dir.glob("*.db*"):
            shutil.copyfile(db_file, target / db_file.name)
    return dest


def bm25_candidate_ids(
    conn: sqlite3.Connection, question: str, limit: int, brain_conf: dict
) -> list[str]:
    spec = hs.build_fts_query(question)
    out = hs.bm25_candidates(
        conn,
        spec,
        columns=BM25_COLUMNS,
        limit=limit,
        weights=brain_conf.get("bm25_weights"),
        max_df_ratio=float(brain_conf.get("bm25_max_df_ratio", 0.5)),
        min_weak_df=int(brain_conf.get("bm25_min_weak_df", 5)),
        scan_limit=int(brain_conf.get("bm25_scan_limit", 500)),
    )
    return [str(r["id"]) for r in out["results"]]


def evaluate(
    run_dir: Path,
    modes: list[str],
    *,
    limit: int = 20,
    override_config: Optional[dict] = None,
    exclude_categories: tuple = (5,),
) -> dict:
    """mode ごとに Recall@k / no-hit を集計する。

    ``bm25`` mode は embedding を呼ばない。``vector`` / ``hybrid`` は
    ``ButlyBrain`` をそのまま使うので、質問1件につき embedding が1回走る。
    """
    rows = load_questions(run_dir)
    provenance = S._load_provenance(run_dir, rows)
    if provenance is None:
        raise ValueError(f"workspace not found under {run_dir}")

    targets = [
        row
        for row in rows
        if int(row.get("category", 0)) not in exclude_categories
        and row.get("evidence")
        and S._oracle_available(row, provenance)
    ]

    results: dict = {"run": str(run_dir), "oracle_questions": len(targets)}
    with tempfile.TemporaryDirectory(prefix="butly-retrieval-replay-") as tmp:
        workspace = mirror_workspace(run_dir, Path(tmp))
        brain = None
        if {"vector", "hybrid"} & set(modes):
            from butly_core.core.brain import ButlyBrain

            brain = ButlyBrain(workspace)

        for mode in modes:
            results[mode] = _evaluate_mode(
                mode,
                targets,
                workspace,
                provenance,
                brain,
                limit,
                override_config or {},
            )
    return results


def _evaluate_mode(
    mode: str,
    targets: list[dict],
    workspace: Path,
    provenance: dict,
    brain,
    limit: int,
    override_config: dict,
) -> dict:
    coverage: dict = {k: [] for k in RECALL_KS}
    hits: dict = {k: 0 for k in RECALL_KS}
    connections: dict = {}
    try:
        for row in targets:
            instance_name = str(row.get("instance_name"))
            if mode == "bm25":
                conn = connections.get(instance_name)
                if conn is None:
                    db_path = instance_db(workspace, instance_name)
                    if db_path is None:
                        continue
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    hs.ensure_fts_index(conn)
                    conn.commit()
                    connections[instance_name] = conn
                brain_conf = dict(override_config.get("brain") or {})
                candidate_ids = bm25_candidate_ids(
                    conn, row["question"], limit, brain_conf
                )
            else:
                conf = _mode_override(override_config, mode)
                diag = brain.quick_vector_search_diag(
                    row["question"],
                    instance_name,
                    limit=limit,
                    threshold=conf["memory_probe"]["vector_search_threshold"],
                    override_config=conf,
                )["diagnostics"]
                candidate_ids = diag.get("fused_candidate_ids") or []

            for k in RECALL_KS:
                value = S._coverage_for_card_ids(
                    row, provenance, candidate_ids[:k]
                ) or 0.0
                coverage[k].append(value)
                hits[k] += value > 0
    finally:
        for conn in connections.values():
            conn.close()

    total = len(targets) or 1
    return {
        **{
            f"recall_at_{k}": sum(coverage[k]) / total for k in RECALL_KS
        },
        **{f"hit_at_{k}": hits[k] for k in RECALL_KS},
        "questions": len(targets),
    }


def _mode_override(override_config: dict, mode: str) -> dict:
    conf = json.loads(json.dumps(override_config)) if override_config else {}
    conf.setdefault("brain", {})["search_mode"] = mode
    probe = conf.setdefault("memory_probe", {})
    probe.setdefault("vector_search_threshold", 0.4)
    return conf


def _load_profile(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    from evals.locomo.config import load_profile

    return dict(load_profile(path).sections)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="対象 run ディレクトリ")
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["bm25"],
        choices=["bm25", "vector", "hybrid"],
        help="比較する検索モード（bm25 は embedding を呼ばない）",
    )
    parser.add_argument("--limit", type=int, default=20, help="取得する候補数")
    parser.add_argument("--profile", type=Path, help="評価 profile YAML（embedding 設定用）")
    parser.add_argument("--out", type=Path, help="結果 JSON の書き出し先")
    args = parser.parse_args(argv)

    result = evaluate(
        args.run,
        args.modes,
        limit=args.limit,
        override_config=_load_profile(args.profile),
    )

    print(f"run: {result['run']}")
    print(f"oracle カードがある問: {result['oracle_questions']}")
    for mode in args.modes:
        stats = result[mode]
        line = " / ".join(
            f"@{k}: {stats[f'recall_at_{k}']:.3f} (hit {stats[f'hit_at_{k}']})"
            for k in RECALL_KS
        )
        print(f"  {mode:7s} recall {line}")
    if args.out:
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
