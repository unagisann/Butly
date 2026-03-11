from housekeeper import ButlyHousekeeper
from butly_core.config import SYSTEM_CONFIG
from pathlib import Path

# 設定からエージェント名・ユーザー名を取得
agent_name = SYSTEM_CONFIG["agent"]["agent_name"]
user_name = SYSTEM_CONFIG["agent"]["user_name"]

# テスト用のダミーテキスト（200文字以上の制限をクリアするため十分な長さ）
dummy_text = f"""
[2026-03-10 10:00:00] {user_name}: {agent_name}、おはよう。今日のタスクを確認したい。
[2026-03-10 10:00:05] {agent_name}: おはようございます。本日は「Gatekeeperのフェーズ3統合」が主なタスクです。要約機能を追加しましょう。
[2026-03-10 10:15:00] {user_name}: わかった、config.py と prompts.py に新しい設定とプロンプトを追加しようか。
[2026-03-10 10:15:10] {agent_name}: かしこまりました。モデルは gemini-3.1-flash-lite-preview を指定しておきますね。
[2026-03-10 11:00:00] {user_name}: いい感じだ。housekeeper.py にもメソッドを追加したぞ。
[2026-03-10 11:00:05] {agent_name}: 素晴らしい進捗です。関係性スナップショットも1週間ごとに更新するロジックが組み込まれました。このペースで進めばプロジェクト完了も間近ですね。
[2026-03-10 11:30:00] {user_name}: よし、これで一回実行してテストしてみるか。
[2026-03-10 11:30:10] {agent_name}: はい、テストの成功を祈っております。
"""

hk = ButlyHousekeeper()
instance_path = Path("butly_core/instances/00_master")
instance_path.mkdir(parents=True, exist_ok=True)

# テスト: Daily Digest 生成
print("Testing daily digest...")
hk._generate_daily_digest(instance_path, dummy_text)

# テスト: 関係性スナップショット更新
print("Testing relationship update...")
hk._update_relationship_if_due(instance_path)

print("Done testing.")
