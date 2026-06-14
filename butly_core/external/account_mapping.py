"""
account_mapping.py
------------------
外部プラットフォーム（Discord / LINE 等）のアカウント／チャンネル／サーバーと、
Butly instance の対応を解決・永続化する。

保存先: ``DATA_DIR/external_accounts.json``

フォーマット例::

    {
      "default_instance": "Butly",
      "discord": {
        "user:123456789012345678": "Jarvis",
        "channel:guild_id:channel_id": "Analyst",
        "guild:guild_id": "Butly"
      }
    }

設計方針:
  - 外部 ID は instance 解決・権限判定にだけ使う。会話本文には混ぜない。
  - SDK（discord.py 等）には一切依存しない純粋ロジック。引数はすべて素の str。
    → 単体テストで discord 未インストールでも検証できる。
  - 書き込みは ``io_utils.atomic_write_text`` で原子的に行う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from butly_core.io_utils import atomic_write_text

# 保存ファイル名（DATA_DIR 直下）
ACCOUNTS_FILENAME = "external_accounts.json"

# external_accounts.json が無い／default_instance が未指定の場合に使う既定 instance。
# 計画書の例に合わせて "Butly"。実在しなければ adapter 側でエラー応答する。
FALLBACK_DEFAULT_INSTANCE = "Butly"

# 対応 scope（解決の優先順位が高い順）
SCOPES = ("user", "channel", "guild")


@dataclass(frozen=True)
class ResolvedInstance:
    """解決結果。

    Attributes
    ----------
    instance_name : str
        採用された Butly instance 名。
    scope : str
        どの scope で解決したか（"user" / "channel" / "guild" / "default"）。
    exists : bool
        その instance が instances ディレクトリに実在するか。
        False の場合、adapter 側でエラー応答すること。
    """

    instance_name: str
    scope: str
    exists: bool


class ExternalAccountStore:
    """``external_accounts.json`` の読み書きと scope 解決を担う。

    インスタンスは bot プロセス内で 1 つだけ持てばよい。読み込みは都度ファイルから
    行い、外部編集にも追従する（個人用ローカル用途なので頻度・競合は無視できる）。

    Parameters
    ----------
    data_dir : Path
        ``external_accounts.json`` を置くディレクトリ（通常 DATA_DIR）。
    instances_dir : Path
        instance 実在チェック用。``instances_dir / <name>`` の存在で判定する。
    """

    def __init__(self, data_dir: Path, instances_dir: Path):
        self.data_dir = Path(data_dir)
        self.instances_dir = Path(instances_dir)
        self.path = self.data_dir / ACCOUNTS_FILENAME

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        """JSON を読み込む。無い／壊れている場合は空構造を返す。"""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[ExternalAccountStore] {self.path} 読み込み失敗、空で続行: {e}")
            return {}
        if not isinstance(data, dict):
            print(f"[ExternalAccountStore] {self.path} が dict でないため無視")
            return {}
        return data

    def _save(self, data: dict) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        atomic_write_text(self.path, text)

    # ------------------------------------------------------------------
    # instance ヘルパー
    # ------------------------------------------------------------------
    def available_instances(self) -> List[str]:
        """利用可能な instance 名一覧（instances ディレクトリを走査）。"""
        if not self.instances_dir.exists():
            return []
        return sorted(
            p.name
            for p in self.instances_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def instance_exists(self, instance_name: str) -> bool:
        if not instance_name:
            return False
        return (self.instances_dir / instance_name).is_dir()

    def default_instance(self) -> Optional[str]:
        """既定 instance を返す。

        - キーが存在しなければ ``FALLBACK_DEFAULT_INSTANCE``。
        - 明示的に ``null`` が設定されている場合は None（= 未登録ユーザー拒否）。
        """
        data = self._load()
        if "default_instance" not in data:
            return FALLBACK_DEFAULT_INSTANCE
        return data.get("default_instance")  # None もそのまま返す

    # ------------------------------------------------------------------
    # 解決
    # ------------------------------------------------------------------
    @staticmethod
    def _user_key(user_id: str) -> str:
        return f"user:{user_id}"

    @staticmethod
    def _channel_key(guild_id: str, channel_id: str) -> str:
        return f"channel:{guild_id}:{channel_id}"

    @staticmethod
    def _guild_key(guild_id: str) -> str:
        return f"guild:{guild_id}"

    @staticmethod
    def _group_user_key(group_id: str, user_id: str) -> str:
        return f"group:{group_id}:{user_id}"

    def resolve_discord(
        self,
        *,
        user_id: Optional[str],
        guild_id: Optional[str],
        channel_id: Optional[str],
    ) -> Optional[ResolvedInstance]:
        """Discord の発話文脈から instance を解決する。

        優先順位::

            user:{user_id}
            → channel:{guild_id}:{channel_id}
            → guild:{guild_id}
            → default_instance

        Returns
        -------
        ResolvedInstance | None
            None は「default_instance が明示的に null で、どの scope にも一致しない」
            （= 未登録ユーザーを拒否する設定）場合のみ。それ以外は必ず結果を返す。
            解決した instance が実在しない場合は ``exists=False`` を立てて返す。
        """
        data = self._load()
        raw_mapping = data.get("discord")
        mapping: Dict[str, str] = raw_mapping if isinstance(raw_mapping, dict) else {}

        # 1) user scope
        if user_id:
            inst = mapping.get(self._user_key(str(user_id)))
            if inst:
                return self._as_resolved(inst, "user")

        # 2) channel scope（guild 内のみ。DM は guild_id=None でスキップ）
        if guild_id and channel_id:
            inst = mapping.get(self._channel_key(str(guild_id), str(channel_id)))
            if inst:
                return self._as_resolved(inst, "channel")

        # 3) guild scope
        if guild_id:
            inst = mapping.get(self._guild_key(str(guild_id)))
            if inst:
                return self._as_resolved(inst, "guild")

        # 4) default
        default = data.get("default_instance", FALLBACK_DEFAULT_INSTANCE)
        if default is None:
            # 明示的に default 無効 → 未登録は拒否
            return None
        if not isinstance(default, str) or not default:
            default = FALLBACK_DEFAULT_INSTANCE
        return self._as_resolved(default, "default")

    def resolve_line(
        self,
        *,
        user_id: Optional[str],
        group_id: Optional[str] = None,
    ) -> Optional[ResolvedInstance]:
        """LINE の発話文脈から、明示的に承認済みの instance を解決する。

        LINE は pairing 必須のため ``default_instance`` にはフォールバックしない。
        グループ固有の割り当てがあれば user 割り当てより優先する。
        """
        if not user_id:
            return None
        data = self._load()
        raw_mapping = data.get("line")
        mapping: Dict[str, str] = raw_mapping if isinstance(raw_mapping, dict) else {}

        if group_id:
            inst = mapping.get(self._group_user_key(str(group_id), str(user_id)))
            if inst:
                return self._as_resolved(inst, "group")

        inst = mapping.get(self._user_key(str(user_id)))
        if inst:
            return self._as_resolved(inst, "user")
        return None

    def _as_resolved(self, instance_name: str, scope: str) -> ResolvedInstance:
        return ResolvedInstance(
            instance_name=instance_name,
            scope=scope,
            exists=self.instance_exists(instance_name),
        )

    def has_channel_binding(
        self, *, guild_id: Optional[str], channel_id: Optional[str]
    ) -> bool:
        """このチャンネルに **channel scope の明示 bind** があるか。

        案B（bound channel での mention なし反応）の判定に使う。guild scope の
        bind はサーバー全体のデフォルト指定であり、ここでは True にしない
        （専用チャンネルだけを自動反応にするため）。DM（guild_id=None）は常に False。
        """
        if not (guild_id and channel_id):
            return False
        data = self._load()
        raw_mapping = data.get("discord")
        mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
        return self._channel_key(str(guild_id), str(channel_id)) in mapping

    # ------------------------------------------------------------------
    # bind / unbind
    # ------------------------------------------------------------------
    def _scope_key(
        self,
        scope: str,
        *,
        user_id: Optional[str],
        guild_id: Optional[str],
        channel_id: Optional[str],
    ) -> str:
        """scope と各 ID から保存キーを組み立てる。

        必要な ID が欠けている場合は ValueError。
        """
        if scope == "user":
            if not user_id:
                raise ValueError("user scope には user_id が必要です")
            return self._user_key(str(user_id))
        if scope == "channel":
            if not (guild_id and channel_id):
                raise ValueError(
                    "channel scope には guild_id と channel_id が必要です"
                    "（DM では channel bind できません）"
                )
            return self._channel_key(str(guild_id), str(channel_id))
        if scope == "guild":
            if not guild_id:
                raise ValueError("guild scope には guild_id が必要です")
            return self._guild_key(str(guild_id))
        raise ValueError(f"未知の scope: {scope!r}（許可: {', '.join(SCOPES)}）")

    def bind_discord(
        self,
        *,
        scope: str,
        instance_name: str,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> str:
        """指定 scope に instance を紐づけて保存する。

        Returns
        -------
        str
            実際に書き込んだ保存キー。

        Raises
        ------
        ValueError
            scope に必要な ID が欠けている / 未知 scope / instance が実在しない。
        """
        if not self.instance_exists(instance_name):
            avail = ", ".join(self.available_instances()) or "(なし)"
            raise ValueError(
                f"instance '{instance_name}' は存在しません。利用可能: {avail}"
            )
        key = self._scope_key(
            scope, user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
        data = self._load()
        discord_map = data.get("discord")
        if not isinstance(discord_map, dict):
            discord_map = {}
        discord_map[key] = instance_name
        data["discord"] = discord_map
        self._save(data)
        return key

    def unbind_discord(
        self,
        *,
        scope: str,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> bool:
        """指定 scope の紐づけを削除する。

        Returns
        -------
        bool
            削除した場合 True、元々無かった場合 False。
        """
        key = self._scope_key(
            scope, user_id=user_id, guild_id=guild_id, channel_id=channel_id
        )
        data = self._load()
        discord_map = data.get("discord")
        if not isinstance(discord_map, dict) or key not in discord_map:
            return False
        del discord_map[key]
        data["discord"] = discord_map
        self._save(data)
        return True

    def bind_external_user(
        self, *, source: str, user_id: str, instance_name: str
    ) -> str:
        """外部サービスの user scope を instance に紐づける。"""
        source = str(source).strip().lower()
        user_id = str(user_id).strip()
        if not source or not user_id:
            raise ValueError("source と user_id は必須です")
        if not self.instance_exists(instance_name):
            avail = ", ".join(self.available_instances()) or "(なし)"
            raise ValueError(
                f"instance '{instance_name}' は存在しません。利用可能: {avail}"
            )

        key = self._user_key(user_id)
        data = self._load()
        mapping = data.get(source)
        if not isinstance(mapping, dict):
            mapping = {}
        mapping[key] = instance_name
        data[source] = mapping
        self._save(data)
        return key
