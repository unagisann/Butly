"""Connection + model 単位の LLM Capability 解決と観測キャッシュ。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any, Literal, Optional

from butly_core.io_utils import atomic_write_text
from butly_core.llm.connections import Connection
from butly_core.llm.model_registry import ModelRef, find_preset

logger = logging.getLogger(__name__)

TokenLimitParameter = Literal[
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
]

_VALID_TOKEN_PARAMETERS = frozenset(
    {"max_tokens", "max_completion_tokens", "max_output_tokens"}
)
_VALID_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


@dataclass(frozen=True)
class ModelCapabilities:
    token_limit_parameter: Optional[TokenLimitParameter] = None
    supports_reasoning: Optional[bool] = None
    reasoning_efforts: Optional[tuple[str, ...]] = None
    default_reasoning_effort: Optional[str] = None
    temperature_supported: Optional[bool] = None
    structured_outputs_supported: Optional[bool] = None
    source: tuple[str, ...] = ()
    observed_at: Optional[str] = None

    def overlay(
        self,
        newer: Optional["ModelCapabilities"],
    ) -> "ModelCapabilities":
        if newer is None:
            return self
        values: dict[str, Any] = {}
        for name in (
            "token_limit_parameter",
            "supports_reasoning",
            "reasoning_efforts",
            "default_reasoning_effort",
            "temperature_supported",
            "structured_outputs_supported",
            "observed_at",
        ):
            new_value = getattr(newer, name)
            values[name] = (
                new_value if new_value is not None else getattr(self, name)
            )
        values["source"] = tuple(dict.fromkeys(self.source + newer.source))
        return ModelCapabilities(**values)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = list(self.source)
        if self.reasoning_efforts is not None:
            data["reasoning_efforts"] = list(self.reasoning_efforts)
        return {key: value for key, value in data.items() if value is not None}


def capabilities_from_mapping(
    raw: Any,
    *,
    source: str,
) -> Optional[ModelCapabilities]:
    if not isinstance(raw, dict):
        return None

    token_parameter = raw.get("token_limit_parameter")
    if token_parameter not in _VALID_TOKEN_PARAMETERS:
        token_parameter = None

    supports_reasoning = raw.get("supports_reasoning")
    if not isinstance(supports_reasoning, bool):
        supports_reasoning = None

    temperature_supported = raw.get("temperature_supported")
    if not isinstance(temperature_supported, bool):
        temperature_supported = raw.get("supports_temperature")
    if not isinstance(temperature_supported, bool):
        temperature_supported = None

    structured = raw.get("structured_outputs_supported")
    if not isinstance(structured, bool):
        structured = raw.get("supports_structured_outputs")
    if not isinstance(structured, bool):
        structured = raw.get("supports_response_schema")
    if not isinstance(structured, bool):
        structured = None

    efforts_raw = raw.get("reasoning_efforts")
    reasoning_efforts: Optional[tuple[str, ...]] = None
    if isinstance(efforts_raw, (list, tuple)):
        valid = [
            str(value)
            for value in efforts_raw
            if str(value) in _VALID_REASONING_EFFORTS
        ]
        reasoning_efforts = tuple(dict.fromkeys(valid)) or None
        if reasoning_efforts and supports_reasoning is None:
            supports_reasoning = True

    default_effort = raw.get("default_reasoning_effort")
    if default_effort not in _VALID_REASONING_EFFORTS:
        default_effort = None
    elif supports_reasoning is None:
        # default effort の公開は reasoning parameter 対応の明示でもある。
        supports_reasoning = True

    supported_parameters = raw.get("supported_parameters")
    if isinstance(supported_parameters, (list, tuple, set)):
        parameters = {str(value) for value in supported_parameters}
        if token_parameter is None:
            if (
                "max_completion_tokens" in parameters
                and "max_tokens" not in parameters
            ):
                token_parameter = "max_completion_tokens"
            elif "max_tokens" in parameters:
                token_parameter = "max_tokens"
        if supports_reasoning is None and "reasoning_effort" in parameters:
            supports_reasoning = True
        if temperature_supported is None:
            temperature_supported = "temperature" in parameters
        if structured is None:
            structured = "response_format" in parameters

    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, str):
        observed_at = None

    capability = ModelCapabilities(
        token_limit_parameter=token_parameter,
        supports_reasoning=supports_reasoning,
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort=default_effort,
        temperature_supported=temperature_supported,
        structured_outputs_supported=structured,
        source=(source,),
        observed_at=observed_at,
    )
    if all(
        value is None
        for value in (
            capability.token_limit_parameter,
            capability.supports_reasoning,
            capability.reasoning_efforts,
            capability.default_reasoning_effort,
            capability.temperature_supported,
            capability.structured_outputs_supported,
        )
    ):
        return None
    return capability


class CapabilityStore:
    """観測に成功した互換設定だけをatomic JSONへ保存する。"""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        except (OSError, json.JSONDecodeError):
            logger.warning("capability cache could not be read", exc_info=True)
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != self.SCHEMA_VERSION
        ):
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if not isinstance(raw.get("entries"), dict):
            raw["entries"] = {}
        return raw

    def get(self, model: ModelRef) -> Optional[ModelCapabilities]:
        with self._lock:
            raw = self._load()
            connection_entries = raw["entries"].get(model.connection_id)
            if not isinstance(connection_entries, dict):
                return None
            return capabilities_from_mapping(
                connection_entries.get(model.model_name),
                source="observed",
            )

    def put(self, model: ModelRef, capabilities: ModelCapabilities) -> None:
        with self._lock:
            raw = self._load()
            entries = raw["entries"].setdefault(model.connection_id, {})
            if not isinstance(entries, dict):
                entries = {}
                raw["entries"][model.connection_id] = entries
            existing = capabilities_from_mapping(
                entries.get(model.model_name),
                source="observed",
            )
            observed = (existing or ModelCapabilities()).overlay(capabilities)
            if observed.observed_at is None:
                observed = replace(
                    observed,
                    observed_at=datetime.now(timezone.utc).isoformat(),
                )
            entries[model.model_name] = observed.to_dict()
            atomic_write_text(
                self.path,
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )

    def invalidate(self, connection_id: Optional[str] = None) -> None:
        with self._lock:
            if not self.path.exists():
                return
            raw = self._load()
            if connection_id is None:
                if not raw["entries"]:
                    return
                raw["entries"] = {}
            else:
                if connection_id not in raw["entries"]:
                    return
                raw["entries"].pop(connection_id, None)
            atomic_write_text(
                self.path,
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_store = CapabilityStore(_PROJECT_ROOT / "llm_capabilities.json")
_manual_overrides: dict[str, Any] = {}
_provider_metadata: dict[tuple[str, str], ModelCapabilities] = {}
_runtime_lock = threading.RLock()


def configure_capability_runtime(
    data_dir: Path,
    manual_overrides: Optional[dict[str, Any]] = None,
) -> None:
    global _store, _manual_overrides
    with _runtime_lock:
        next_path = Path(data_dir) / "llm_capabilities.json"
        if _store.path != next_path:
            _provider_metadata.clear()
        _store = CapabilityStore(next_path)
        _manual_overrides = dict(manual_overrides or {})


def register_provider_metadata(
    model: ModelRef,
    metadata: Any,
) -> None:
    capability = capabilities_from_mapping(
        metadata,
        source="provider_metadata",
    )
    if capability is None:
        return
    with _runtime_lock:
        key = (model.connection_id, model.model_name)
        _provider_metadata[key] = capability


def invalidate_provider_metadata(connection_id: Optional[str] = None) -> None:
    """Provider catalog由来のin-memory metadataだけを破棄する。"""
    with _runtime_lock:
        if connection_id is None:
            _provider_metadata.clear()
            return
        for key in list(_provider_metadata):
            if key[0] == connection_id:
                _provider_metadata.pop(key, None)


def invalidate_capability_cache(connection_id: Optional[str] = None) -> None:
    with _runtime_lock:
        _store.invalidate(connection_id)
        invalidate_provider_metadata(connection_id)


def _adapter_defaults(connection: Connection) -> ModelCapabilities:
    if connection.protocol == "gemini_native":
        return ModelCapabilities(
            token_limit_parameter="max_output_tokens",
            temperature_supported=True,
            source=("adapter_default",),
        )
    if connection.id == "openai":
        # OpenAI Chat Completions の現行共通名。モデルごとの差分は
        # metadata / preset / observed capability で上書きする。
        return ModelCapabilities(
            token_limit_parameter="max_completion_tokens",
            source=("adapter_default",),
        )
    return ModelCapabilities(
        token_limit_parameter="max_tokens",
        temperature_supported=True,
        source=("adapter_default",),
    )


def _preset_capabilities(
    connection: Connection,
    model: ModelRef,
) -> Optional[ModelCapabilities]:
    preset = find_preset(model.connection_id, model.model_name)
    if preset is None:
        return None

    advertises_reasoning = "reasoning" in preset.capabilities
    supports_reasoning: Optional[bool] = None
    reasoning_efforts: Optional[tuple[str, ...]] = None
    temperature_supported: Optional[bool] = None
    token_limit_parameter: Optional[TokenLimitParameter] = None
    default_reasoning_effort: Optional[str] = None
    if connection.id == "openai":
        supports_reasoning = advertises_reasoning
        token_limit_parameter = "max_completion_tokens"
        temperature_supported = not advertises_reasoning
        if advertises_reasoning:
            default_reasoning_effort = "medium"
    base = ModelCapabilities(
        supports_reasoning=supports_reasoning,
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort=default_reasoning_effort,
        temperature_supported=temperature_supported,
        token_limit_parameter=token_limit_parameter,
        structured_outputs_supported=(
            "structured_outputs" in preset.capabilities
        ),
        source=("model_registry",),
    )
    configured = capabilities_from_mapping(
        preset.default_params.get("capabilities"),
        source="model_registry_params",
    )
    return base.overlay(configured)


def _manual_capabilities(model: ModelRef) -> Optional[ModelCapabilities]:
    connection_raw = _manual_overrides.get(model.connection_id)
    if not isinstance(connection_raw, dict):
        return None
    return capabilities_from_mapping(
        connection_raw.get(model.model_name),
        source="manual_override",
    )


class CapabilityResolver:
    """Adapter既定値から実測・手動overrideまでをfield単位で重ねる。"""

    def resolve(
        self,
        connection: Connection,
        model: ModelRef,
    ) -> ModelCapabilities:
        resolved = _adapter_defaults(connection)
        resolved = resolved.overlay(_preset_capabilities(connection, model))
        with _runtime_lock:
            resolved = resolved.overlay(
                _provider_metadata.get((model.connection_id, model.model_name))
            )
            resolved = resolved.overlay(_store.get(model))
            resolved = resolved.overlay(_manual_capabilities(model))
        return resolved

    def record_observed(
        self,
        model: ModelRef,
        capabilities: ModelCapabilities,
    ) -> None:
        observed = replace(
            capabilities,
            source=("observed",),
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        with _runtime_lock:
            _store.put(model, observed)


_resolver = CapabilityResolver()


def get_capability_resolver() -> CapabilityResolver:
    return _resolver


__all__ = [
    "CapabilityResolver",
    "CapabilityStore",
    "ModelCapabilities",
    "TokenLimitParameter",
    "capabilities_from_mapping",
    "configure_capability_runtime",
    "get_capability_resolver",
    "invalidate_capability_cache",
    "invalidate_provider_metadata",
    "register_provider_metadata",
]
