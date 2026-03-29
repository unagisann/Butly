"""
マイルストーンを作成し、Issueを割り当てるスクリプト

.github/MILESTONES/milestones.json を読み込み、
GitHub APIでマイルストーンを作成し、タイトルでIssueを検索して割り当てる。

使い方:
    python .github/scripts/setup_milestones.py

環境変数:
    GITHUB_REPOSITORY: リポジトリ名 (owner/repo形式)
"""

import json
import subprocess
import sys
from pathlib import Path


def run_gh(args: list[str], repo: str | None = None) -> str:
    """gh CLIコマンドを実行して標準出力を返す。"""
    cmd = ["gh"] + args
    if repo:
        cmd.extend(["--repo", repo])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def get_existing_milestones(repo: str | None = None) -> dict[str, int]:
    """既存マイルストーンの {title: number} マップを取得する。"""
    try:
        output = run_gh(
            ["api", "repos/{owner}/{repo}/milestones", "--paginate",
             "--jq", '.[] | "\(.number)\t\(.title)"'],
            repo,
        )
    except subprocess.CalledProcessError:
        return {}

    milestones = {}
    for line in output.strip().split("\n"):
        if "\t" in line:
            number, title = line.split("\t", 1)
            milestones[title] = int(number)
    return milestones


def create_milestone(title: str, description: str, due_on: str | None,
                     repo: str | None = None) -> int:
    """マイルストーンを作成し、番号を返す。"""
    body_dict = {"title": title, "description": description, "state": "open"}
    if due_on:
        body_dict["due_on"] = due_on
    body = json.dumps(body_dict)

    cmd = ["gh", "api", "repos/{owner}/{repo}/milestones",
           "--method", "POST", "--input", "-", "--jq", ".number"]
    if repo:
        cmd.extend(["--repo", repo])
    result = subprocess.run(
        cmd, input=body, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


def find_issue_number(title: str, repo: str | None = None) -> int | None:
    """タイトルでIssueを検索し、issue番号を返す。"""
    try:
        output = run_gh(
            ["issue", "list", "--search", f'"{title}" in:title',
             "--json", "number,title", "--limit", "50"],
            repo,
        )
        issues = json.loads(output)
        for issue in issues:
            if issue["title"] == title:
                return issue["number"]
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return None


def assign_issue_to_milestone(issue_number: int, milestone_number: int,
                              repo: str | None = None) -> bool:
    """IssueにマイルストーンをPATCHで割り当てる。"""
    body = json.dumps({"milestone": milestone_number})
    try:
        subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{issue_number}",
             "--method", "PATCH", "--input", "-"]
            + (["--repo", repo] if repo else []),
            input=body, capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Issue #{issue_number} への割り当て失敗: {e.stderr}")
        return False


def main() -> None:
    milestones_file = Path(".github/MILESTONES/milestones.json")
    if not milestones_file.exists():
        print("❌ milestones.json が見つかりません")
        sys.exit(1)

    import os
    repo = os.environ.get("GITHUB_REPOSITORY")

    milestones_config = json.loads(milestones_file.read_text(encoding="utf-8"))
    existing = get_existing_milestones(repo)

    print(f"📋 {len(milestones_config)} 個のマイルストーンを処理します\n")

    for ms in milestones_config:
        title = ms["title"]
        description = ms.get("description", "")
        due_on = ms.get("due_on")
        issue_titles = ms.get("issue_titles", [])

        # マイルストーン作成 or 既存取得
        if title in existing:
            ms_number = existing[title]
            print(f"✅ 既存: {title} (#{ms_number})")
        else:
            try:
                ms_number = create_milestone(title, description, due_on, repo)
                print(f"🆕 作成: {title} (#{ms_number})")
            except subprocess.CalledProcessError as e:
                print(f"❌ 作成失敗: {title} — {e.stderr}")
                continue

        # Issue割り当て
        for it in issue_titles:
            issue_num = find_issue_number(it, repo)
            if issue_num is None:
                print(f"  ⚠️ Issue未発見: {it}")
                continue
            if assign_issue_to_milestone(issue_num, ms_number, repo):
                print(f"  📌 割当: #{issue_num} {it}")


if __name__ == "__main__":
    main()
