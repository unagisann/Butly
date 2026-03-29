---
title: "サンプルIssue: MDファイルからのIssue作成テスト"
labels:
  - documentation
assignees:
  - unagisann
---

## 概要

これはMDファイルからIssueを作成する機能のサンプルです。

## 説明

`.github/ISSUES/` ディレクトリにMarkdownファイルを配置し、pushすると自動的にGitHub Issueが作成されます。

### フォーマット

- YAML フロントマター（`---` で囲む）にメタデータを記述
- フロントマター以降がIssueの本文

## 確認事項

- [ ] Issueが正しく作成されること
- [ ] ラベルが正しく設定されること
- [ ] アサインが正しく設定されること
