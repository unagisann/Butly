from housekeeper import ButlyHousekeeper
from pathlib import Path

# Provide a reasonably long dummy text to bypass the 200 chars limit for summary generation
dummy_text = """
[2026-03-10 10:00:00] 主人: ジャービス、おはよう。今日のタスクを確認したい。
[2026-03-10 10:00:05] Jarvis: おはようございます、ご主人様。本日は「Gatekeeperのフェーズ3統合」が主なタスクです。要約機能を追加しましょう。
[2026-03-10 10:15:00] 主人: わかった、config.py と prompts.py に新しい設定とプロンプトを追加しようか。
[2026-03-10 10:15:10] Jarvis: かしこまりました。モデルは gemini-3.1-flash-lite-preview を指定しておきますね。
[2026-03-10 11:00:00] 主人: いい感じだ。housekeeper.py にもメソッドを追加したぞ。
[2026-03-10 11:00:05] Jarvis: 素晴らしい進捗です。関係性スナップショットも1週間ごとに更新するロジックが組み込まれました。このペースで進めばプロジェクト完了も間近ですね。
[2026-03-10 11:30:00] 主人: よし、これで一回実行してテストしてみるか。
[2026-03-10 11:30:10] Jarvis: はい、テストの成功を祈っております。私の所感としては、主人の手際が相変わらず良くて頼もしい限りです。
"""

hk = ButlyHousekeeper()
instance_path = Path("butly_core/instances/00_master")
instance_path.mkdir(parents=True, exist_ok=True)

# Test daily digest directly
print("Testing daily digest...")
hk._generate_daily_digest(instance_path, dummy_text)

# Test relationship update directly
print("Testing relationship update...")
hk._update_relationship_if_due(instance_path)

print("Done testing.")
