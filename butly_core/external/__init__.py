"""
butly_core.external
--------------------
外部チャット入口（Discord / 将来 LINE 等）の adapter 層。

中核チャット処理（ButlyRuntime / ChatService / Memory / Gatekeeper / RAG）には
極力手を入れず、各プラットフォーム固有の「受信イベント正規化・instance 解決・
返信整形・送信」だけをここに閉じ込める。

discord.py 等の optional 依存はこのパッケージのトップレベルでは import しない。
SDK を使うコードは ``discord_adapter.create_bot()`` のような関数内で遅延 import する
ことで、SDK 未インストール環境でも Butly 本体・単体テストが壊れないようにする。
"""
