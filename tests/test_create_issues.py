"""create_issues.py の parse_frontmatter と _parse_yaml_simple のテスト"""

import sys
from pathlib import Path

# .github/scripts をパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".github" / "scripts"))

from create_issues import _parse_yaml_simple, parse_frontmatter


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
