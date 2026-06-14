"""
外部チャットアカウントの期限付き pairing code 管理。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.io_utils import atomic_write_text

PAIRINGS_FILENAME = "pending_pairings.json"
_PAIRING_LOCK = RLock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PendingPairing:
    code: str
    source: str
    external_user_id: str
    external_channel_id: Optional[str]
    created_at: str
    expires_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class PairingStore:
    def __init__(
        self,
        data_dir: Path,
        account_store: ExternalAccountStore,
        *,
        ttl_seconds: int = 600,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.path = Path(data_dir) / PAIRINGS_FILENAME
        self.account_store = account_store
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        # request ごとに PairingStore が生成されても同一プロセス内の更新を直列化する。
        self._lock = _PAIRING_LOCK

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        atomic_write_text(
            self.path, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _active(self, raw: dict, now: datetime) -> bool:
        try:
            required = ("source", "external_user_id", "created_at", "expires_at")
            if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
                return False
            return _parse_time(raw["expires_at"]) > now
        except (KeyError, TypeError, ValueError):
            return False

    def _cleanup(self, data: dict[str, dict], now: datetime) -> bool:
        expired = [code for code, raw in data.items() if not self._active(raw, now)]
        for code in expired:
            del data[code]
        return bool(expired)

    @staticmethod
    def _record(code: str, raw: dict) -> PendingPairing:
        return PendingPairing(code=code, **raw)

    def issue(
        self,
        *,
        source: str,
        external_user_id: str,
        external_channel_id: Optional[str] = None,
    ) -> PendingPairing:
        """有効な既存 code があれば再利用し、無ければ 6 桁 code を発行する。"""
        source = str(source).strip().lower()
        external_user_id = str(external_user_id).strip()
        if not source or not external_user_id:
            raise ValueError("source と external_user_id は必須です")

        with self._lock:
            now = self.clock()
            data = self._load()
            changed = self._cleanup(data, now)
            for code, raw in data.items():
                if (
                    raw.get("source") == source
                    and raw.get("external_user_id") == external_user_id
                ):
                    if changed:
                        self._save(data)
                    return self._record(code, raw)

            for _ in range(100):
                code = f"{secrets.randbelow(1_000_000):06d}"
                if code not in data:
                    break
            else:
                raise RuntimeError("pairing code を発行できませんでした")

            expires_at = now + timedelta(seconds=self.ttl_seconds)
            raw = {
                "source": source,
                "external_user_id": external_user_id,
                "external_channel_id": (
                    str(external_channel_id)
                    if external_channel_id is not None
                    else None
                ),
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            data[code] = raw
            self._save(data)
            return self._record(code, raw)

    def pending(self) -> list[PendingPairing]:
        with self._lock:
            now = self.clock()
            data = self._load()
            if self._cleanup(data, now):
                self._save(data)
            records = [self._record(code, raw) for code, raw in data.items()]
            return sorted(records, key=lambda record: record.created_at)

    def approve(self, code: str, instance_name: Optional[str] = None) -> PendingPairing:
        with self._lock:
            now = self.clock()
            data = self._load()
            changed = self._cleanup(data, now)
            raw = data.get(str(code))
            if raw is None:
                if changed:
                    self._save(data)
                raise KeyError("pairing code が無効または期限切れです")

            target = instance_name or self.account_store.default_instance()
            if not target:
                raise ValueError("承認先 instance を指定してください")
            record = self._record(str(code), raw)
            self.account_store.bind_external_user(
                source=record.source,
                user_id=record.external_user_id,
                instance_name=target,
            )
            del data[str(code)]
            self._save(data)
            return record

    def reject(self, code: str) -> PendingPairing:
        with self._lock:
            now = self.clock()
            data = self._load()
            changed = self._cleanup(data, now)
            raw = data.get(str(code))
            if raw is None:
                if changed:
                    self._save(data)
                raise KeyError("pairing code が無効または期限切れです")
            record = self._record(str(code), raw)
            del data[str(code)]
            self._save(data)
            return record
