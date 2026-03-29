"""create_issues.py のテスト"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# .github/scripts をパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".github" / "scripts"))

import subprocess

from create_issues import (
    _parse_yaml_simple,
    create_issue_with_gh,
    main,
    parse_frontmatter,
    process_issue_files,
)


class TestParseYamlSimple:
    """_parse_yaml_simple のテスト"""

    def test_simple_key_value(self):
        text = 'title: "テストタイトル"'
        result = _parse_yaml_simple(text)
        assert result["title"] == "テストタイトル"

    def test_key_value_without_quotes(self):
        text = "title: テストタイトル"
        result = _parse_yaml_simple(text)
        assert result["title"] == "テストタイトル"

    def test_list_values(self):
        text = "labels:\n  - bug\n  - enhancement"
        result = _parse_yaml_simple(text)
        assert result["labels"] == ["bug", "enhancement"]

    def test_mixed_values(self):
        text = 'title: "テスト"\nlabels:\n  - bug\nassignees:\n  - user1\n  - user2'
        result = _parse_yaml_simple(text)
        assert result["title"] == "テスト"
        assert result["labels"] == ["bug"]
        assert result["assignees"] == ["user1", "user2"]

    def test_empty_input(self):
        result = _parse_yaml_simple("")
        assert result == {}

    def test_comments_ignored(self):
        text = "# コメント\ntitle: テスト"
        result = _parse_yaml_simple(text)
        assert result["title"] == "テスト"

    def test_milestone_value(self):
        text = 'milestone: "v1.0"'
        result = _parse_yaml_simple(text)
        assert result["milestone"] == "v1.0"

    def test_single_quotes(self):
        text = "title: 'シングルクォート'"
        result = _parse_yaml_simple(text)
        assert result["title"] == "シングルクォート"


class TestParseFrontmatter:
    """parse_frontmatter のテスト"""

    def test_basic_frontmatter(self):
        content = '---\ntitle: "テストIssue"\nlabels:\n  - bug\n---\n\n## 本文\n\nテスト内容'
        metadata, body = parse_frontmatter(content)
        assert metadata["title"] == "テストIssue"
        assert metadata["labels"] == ["bug"]
        assert "## 本文" in body
        assert "テスト内容" in body

    def test_no_frontmatter(self):
        content = "# ただのMarkdown\n\n内容"
        try:
            parse_frontmatter(content)
            assert False, "ValueErrorが発生すべき"
        except ValueError as e:
            assert "フロントマター" in str(e)

    def test_unclosed_frontmatter(self):
        content = "---\ntitle: テスト\n\n本文"
        try:
            parse_frontmatter(content)
            assert False, "ValueErrorが発生すべき"
        except ValueError as e:
            assert "終端" in str(e)

    def test_empty_body(self):
        content = "---\ntitle: テスト\n---"
        metadata, body = parse_frontmatter(content)
        assert metadata["title"] == "テスト"
        assert body == ""

    def test_body_with_multiple_sections(self):
        content = '---\ntitle: "テスト"\n---\n\n## セクション1\n\n内容1\n\n## セクション2\n\n内容2'
        metadata, body = parse_frontmatter(content)
        assert "セクション1" in body
        assert "セクション2" in body

    def test_template_file(self):
        """実際のテンプレートファイルをパースできることを確認"""
        template_path = (
            Path(__file__).resolve().parent.parent / ".github" / "ISSUES" / "_template.md"
        )
        if template_path.exists():
            content = template_path.read_text(encoding="utf-8")
            # テンプレートファイルの先頭コメント部分をスキップ
            # フロントマターの開始位置を見つける
            lines = content.split("\n")
            frontmatter_start = None
            for i, line in enumerate(lines):
                if line.strip() == "---":
                    frontmatter_start = i
                    break
            if frontmatter_start is not None:
                trimmed = "\n".join(lines[frontmatter_start:])
                metadata, body = parse_frontmatter(trimmed)
                assert "title" in metadata

    def test_sample_issue_file(self):
        """実際のサンプルIssueファイルをパースできることを確認"""
        sample_path = (
            Path(__file__).resolve().parent.parent / ".github" / "ISSUES" / "sample-issue.md"
        )
        if sample_path.exists():
            content = sample_path.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(content)
            assert "title" in metadata
            assert metadata["title"] != ""
            assert len(body) > 0

    def test_frontmatter_with_leading_comments(self):
        """先頭にコメント行がある場合でもパースできることを確認"""
        content = '# コメント行\n# もう一つのコメント\n\n---\ntitle: "テスト"\n---\n\n本文'
        metadata, body = parse_frontmatter(content)
        assert metadata["title"] == "テスト"
        assert "本文" in body


class TestCreateIssueWithGh:
    """create_issue_with_gh のテスト"""

    @patch("create_issues.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")
        result = create_issue_with_gh(title="テスト", body="本文")
        assert result is not None
        assert result["url"] == "https://github.com/owner/repo/issues/1"
        mock_run.assert_called_once()

    @patch("create_issues.subprocess.run")
    def test_with_labels_and_assignees(self, mock_run):
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/2\n")
        result = create_issue_with_gh(
            title="テスト",
            body="本文",
            labels=["bug", "enhancement"],
            assignees=["user1"],
            milestone="v1.0",
            repo="owner/repo",
        )
        assert result is not None
        cmd = mock_run.call_args[0][0]
        assert "--label" in cmd
        assert "bug" in cmd
        assert "enhancement" in cmd
        assert "--assignee" in cmd
        assert "user1" in cmd
        assert "--milestone" in cmd
        assert "v1.0" in cmd
        assert "--repo" in cmd
        assert "owner/repo" in cmd

    @patch("create_issues.subprocess.run")
    def test_called_process_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="API error")
        result = create_issue_with_gh(title="テスト", body="本文")
        assert result is None

    @patch("create_issues.subprocess.run")
    def test_gh_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        result = create_issue_with_gh(title="テスト", body="本文")
        assert result is None


class TestProcessIssueFiles:
    """process_issue_files のテスト"""

    def test_nonexistent_directory(self):
        success, fail = process_issue_files("/nonexistent/path")
        assert success == 0
        assert fail == 0

    @patch("create_issues.create_issue_with_gh")
    def test_process_files(self, mock_create, tmp_path):
        # テスト用MDファイルを作成
        issue_file = tmp_path / "test-issue.md"
        issue_file.write_text('---\ntitle: "テスト"\nlabels:\n  - bug\n---\n\n本文', encoding="utf-8")

        # _で始まるファイル（除外対象）
        template_file = tmp_path / "_template.md"
        template_file.write_text('---\ntitle: "テンプレ"\n---\n\n本文', encoding="utf-8")

        mock_create.return_value = {"url": "https://github.com/owner/repo/issues/1"}
        success, fail = process_issue_files(str(tmp_path))
        assert success == 1
        assert fail == 0
        # _template.md は処理されない
        mock_create.assert_called_once()

    @patch("create_issues.create_issue_with_gh")
    def test_missing_title(self, mock_create, tmp_path):
        issue_file = tmp_path / "no-title.md"
        issue_file.write_text("---\nlabels:\n  - bug\n---\n\n本文", encoding="utf-8")
        success, fail = process_issue_files(str(tmp_path))
        assert success == 0
        assert fail == 1
        mock_create.assert_not_called()

    @patch("create_issues.create_issue_with_gh")
    def test_invalid_frontmatter(self, mock_create, tmp_path):
        issue_file = tmp_path / "bad.md"
        issue_file.write_text("これはフロントマターなし", encoding="utf-8")
        success, fail = process_issue_files(str(tmp_path))
        assert success == 0
        assert fail == 1

    def test_empty_directory(self, tmp_path):
        success, fail = process_issue_files(str(tmp_path))
        assert success == 0
        assert fail == 0

    @patch("create_issues.create_issue_with_gh")
    def test_gh_failure(self, mock_create, tmp_path):
        issue_file = tmp_path / "fail.md"
        issue_file.write_text('---\ntitle: "テスト"\n---\n\n本文', encoding="utf-8")
        mock_create.return_value = None  # 失敗
        success, fail = process_issue_files(str(tmp_path))
        assert success == 0
        assert fail == 1


class TestMain:
    """main のテスト"""

    @patch("create_issues.process_issue_files")
    def test_main_no_issues(self, mock_process):
        mock_process.return_value = (0, 0)
        # 例外なしで終了すること
        main()
        mock_process.assert_called_once()

    @patch("create_issues.process_issue_files")
    def test_main_with_failures_exits(self, mock_process):
        mock_process.return_value = (1, 1)
        try:
            main()
            assert False, "SystemExitが発生すべき"
        except SystemExit as e:
            assert e.code == 1

    @patch("create_issues.process_issue_files")
    def test_main_uses_env_vars(self, mock_process):
        mock_process.return_value = (1, 0)
        with patch.dict(os.environ, {"ISSUES_DIR": "/custom/path", "GITHUB_REPOSITORY": "owner/repo"}):
            main()
        mock_process.assert_called_once_with("/custom/path", "owner/repo")
