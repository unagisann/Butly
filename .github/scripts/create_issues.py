"""
MDファイルからGitHub Issueを作成するスクリプト

.github/ISSUES/ ディレクトリ内のMarkdownファイルを読み込み、
YAMLフロントマターを解析してGitHub Issueを作成します。

使い方:
    python .github/scripts/create_issues.py

環境変数:
    GITHUB_TOKEN: GitHub APIトークン
    GITHUB_REPOSITORY: リポジトリ名 (owner/repo形式)
    ISSUES_DIR: Issueファイルのディレクトリパス (デフォルト: .github/ISSUES)
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """MarkdownファイルからYAMLフロントマターと本文を分離する。

    PyYAMLに依存せず、シンプルなパーサーで処理する。

    Args:
        content: Markdownファイルの全内容

    Returns:
        (メタデータ辞書, 本文文字列) のタプル

    Raises:
        ValueError: フロントマターが不正な場合
    """
    content = content.strip()

    # 先頭のコメント行（# で始まる行や空行）をスキップ
    lines = content.split("\n")
    start_line = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            start_line = i
            break
        if stripped and not stripped.startswith("#"):
            raise ValueError("フロントマターが見つかりません（先頭に --- が必要です）")

    content = "\n".join(lines[start_line:]).strip()

    if not content.startswith("---"):
        raise ValueError("フロントマターが見つかりません（先頭に --- が必要です）")

    # 2つ目の --- を探す
    end_index = content.find("---", 3)
    if end_index == -1:
        raise ValueError("フロントマターの終端 --- が見つかりません")

    frontmatter_text = content[3:end_index].strip()
    body = content[end_index + 3:].strip()

    metadata = _parse_yaml_simple(frontmatter_text)
    return metadata, body


def _parse_yaml_simple(text: str) -> dict:
    """シンプルなYAMLパーサー。title, labels, assignees, milestoneをサポート。

    Args:
        text: フロントマター内のYAMLテキスト

    Returns:
        解析結果の辞書
    """
    result = {}
    current_key = None
    current_list = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # リスト項目の処理
        if stripped.startswith("- ") and current_key is not None:
            value = stripped[2:].strip().strip('"').strip("'")
            current_list.append(value)
            result[current_key] = current_list
            continue

        # キー: 値 の処理
        if ":" in stripped:
            # 前のリストキーを保存
            colon_index = stripped.index(":")
            key = stripped[:colon_index].strip()
            value = stripped[colon_index + 1:].strip().strip('"').strip("'")

            current_key = key
            if value:
                # インライン値
                result[key] = value
                current_list = []
                current_key = None
            else:
                # リストの開始
                current_list = []

    return result


def ensure_labels_exist(labels: list[str], repo: str | None = None) -> None:
    """ラベルが存在しない場合は自動作成する。

    Args:
        labels: 確認・作成するラベルのリスト
        repo: リポジトリ名 (owner/repo形式)
    """
    if not labels:
        return

    # 既存ラベル一覧を取得
    cmd = ["gh", "label", "list", "--json", "name", "--limit", "200"]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        existing = {item["name"] for item in json.loads(result.stdout)}
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        existing = set()

    for label in labels:
        if label not in existing:
            create_cmd = ["gh", "label", "create", label]
            if repo:
                create_cmd.extend(["--repo", repo])
            try:
                subprocess.run(create_cmd, capture_output=True, text=True, check=True)
                print(f"  🏷️ ラベル作成: {label}")
            except subprocess.CalledProcessError:
                print(f"  ⚠️ ラベル作成スキップ (既存?): {label}")


def create_issue_with_gh(
    title: str,
    body: str,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone: str | None = None,
    repo: str | None = None,
) -> dict | None:
    """gh CLIを使用してGitHub Issueを作成する。

    Args:
        title: Issueのタイトル
        body: Issueの本文
        labels: ラベルのリスト
        assignees: アサインするユーザーのリスト
        milestone: マイルストーン名
        repo: リポジトリ名 (owner/repo形式)

    Returns:
        作成されたIssueの情報(dict)、失敗時はNone
    """
    # ラベルが存在しない場合は事前に作成
    if labels:
        ensure_labels_exist(labels, repo)

    cmd = ["gh", "issue", "create", "--title", title, "--body", body]

    if labels:
        for label in labels:
            cmd.extend(["--label", label])

    if assignees:
        for assignee in assignees:
            cmd.extend(["--assignee", assignee])

    if milestone:
        cmd.extend(["--milestone", milestone])

    if repo:
        cmd.extend(["--repo", repo])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        issue_url = result.stdout.strip()
        print(f"  ✅ Issue作成成功: {issue_url}")
        return {"url": issue_url}
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Issue作成失敗: {e.stderr.strip()}")
        return None
    except FileNotFoundError:
        print("  ❌ gh CLI が見つかりません。GitHub CLI をインストールしてください。")
        return None


def process_issue_files(issues_dir: str, repo: str | None = None) -> tuple[int, int]:
    """指定ディレクトリ内のMDファイルを処理してIssueを作成する。

    Args:
        issues_dir: Issueファイルが格納されたディレクトリパス
        repo: リポジトリ名 (owner/repo形式)

    Returns:
        (成功数, 失敗数) のタプル
    """
    issues_path = Path(issues_dir)
    if not issues_path.is_dir():
        print(f"❌ ディレクトリが見つかりません: {issues_dir}")
        return 0, 0

    md_files = sorted(issues_path.glob("*.md"))
    # _で始まるファイル（テンプレート等）を除外
    md_files = [f for f in md_files if not f.name.startswith("_")]

    if not md_files:
        print("📭 処理対象のMDファイルがありません。")
        return 0, 0

    print(f"📋 {len(md_files)} 件のMDファイルを検出しました。\n")

    success_count = 0
    fail_count = 0

    for md_file in md_files:
        print(f"📄 処理中: {md_file.name}")

        try:
            content = md_file.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(content)
        except (ValueError, OSError) as e:
            print(f"  ❌ パースエラー: {e}")
            fail_count += 1
            continue

        title = metadata.get("title")
        if not title:
            print("  ❌ タイトルが指定されていません。")
            fail_count += 1
            continue

        labels = metadata.get("labels")
        if isinstance(labels, str):
            labels = [l.strip() for l in labels.split(",")]

        assignees = metadata.get("assignees")
        if isinstance(assignees, str):
            assignees = [a.strip() for a in assignees.split(",")]

        milestone = metadata.get("milestone")

        result = create_issue_with_gh(
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
            milestone=milestone,
            repo=repo,
        )

        if result:
            success_count += 1
        else:
            fail_count += 1

    print(f"\n📊 結果: 成功 {success_count} 件, 失敗 {fail_count} 件")
    return success_count, fail_count


def main() -> None:
    """メインエントリーポイント。"""
    issues_dir = os.environ.get("ISSUES_DIR", ".github/ISSUES")
    repo = os.environ.get("GITHUB_REPOSITORY")

    print("🚀 MDファイルからIssueを作成します...\n")

    success, fail = process_issue_files(issues_dir, repo)

    if fail > 0:
        sys.exit(1)

    if success == 0:
        print("ℹ️  作成対象のIssueはありませんでした。")


if __name__ == "__main__":
    main()
