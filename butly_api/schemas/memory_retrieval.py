"""Typed contract for global and per-instance memory retrieval settings."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt


SearchMode = Literal["vector", "hybrid", "hybrid_evidence_fusion"]
RagSourceMode = Literal["cards", "raw", "both"]
SettingOrigin = Literal["default", "global", "instance"]


class _StrictSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MemoryRetrievalValues(_StrictSettingsModel):
    search_mode: SearchMode
    vector_search_limit: StrictInt = Field(ge=1, le=10)
    evidence_fusion_base_weight: StrictFloat = Field(ge=0.0, le=1.0)
    evidence_raw_chunk_chars: StrictInt = Field(ge=200, le=10000)
    vector_candidates: StrictInt = Field(ge=3, le=100)
    bm25_candidates: StrictInt = Field(ge=3, le=100)
    rag_source_mode: RagSourceMode
    rag_raw_top_k: StrictInt = Field(ge=0, le=20)
    rag_raw_max_chars: StrictInt = Field(ge=0, le=50000)
    rag_raw_neighbor_radius: StrictInt = Field(ge=0, le=10)


class MemoryRetrievalGlobalPatch(_StrictSettingsModel):
    """Partial global update. Explicit null is invalid."""

    search_mode: SearchMode = Field(default=None)
    vector_search_limit: StrictInt = Field(default=None, ge=1, le=10)
    evidence_fusion_base_weight: StrictFloat = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    evidence_raw_chunk_chars: StrictInt = Field(default=None, ge=200, le=10000)
    vector_candidates: StrictInt = Field(default=None, ge=3, le=100)
    bm25_candidates: StrictInt = Field(default=None, ge=3, le=100)
    rag_source_mode: RagSourceMode = Field(default=None)
    rag_raw_top_k: StrictInt = Field(default=None, ge=0, le=20)
    rag_raw_max_chars: StrictInt = Field(default=None, ge=0, le=50000)
    rag_raw_neighbor_radius: StrictInt = Field(default=None, ge=0, le=10)


class MemoryRetrievalInstancePatch(_StrictSettingsModel):
    """Partial instance update. Null removes that instance override."""

    search_mode: Optional[SearchMode] = None
    vector_search_limit: Optional[StrictInt] = Field(default=None, ge=1, le=10)
    evidence_fusion_base_weight: Optional[StrictFloat] = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    evidence_raw_chunk_chars: Optional[StrictInt] = Field(
        default=None,
        ge=200,
        le=10000,
    )
    vector_candidates: Optional[StrictInt] = Field(default=None, ge=3, le=100)
    bm25_candidates: Optional[StrictInt] = Field(default=None, ge=3, le=100)
    rag_source_mode: Optional[RagSourceMode] = None
    rag_raw_top_k: Optional[StrictInt] = Field(default=None, ge=0, le=20)
    rag_raw_max_chars: Optional[StrictInt] = Field(
        default=None,
        ge=0,
        le=50000,
    )
    rag_raw_neighbor_radius: Optional[StrictInt] = Field(
        default=None,
        ge=0,
        le=10,
    )


class MemoryRetrievalOrigins(_StrictSettingsModel):
    search_mode: SettingOrigin
    vector_search_limit: SettingOrigin
    evidence_fusion_base_weight: SettingOrigin
    evidence_raw_chunk_chars: SettingOrigin
    vector_candidates: SettingOrigin
    bm25_candidates: SettingOrigin
    rag_source_mode: SettingOrigin
    rag_raw_top_k: SettingOrigin
    rag_raw_max_chars: SettingOrigin
    rag_raw_neighbor_radius: SettingOrigin


class GlobalMemoryRetrievalSettingsResponse(_StrictSettingsModel):
    defaults: MemoryRetrievalValues
    global_override: MemoryRetrievalGlobalPatch
    effective: MemoryRetrievalValues
    origins: MemoryRetrievalOrigins


class InstanceMemoryRetrievalSettingsResponse(_StrictSettingsModel):
    defaults: MemoryRetrievalValues
    global_override: MemoryRetrievalGlobalPatch
    global_effective: MemoryRetrievalValues
    instance_override: MemoryRetrievalInstancePatch
    effective: MemoryRetrievalValues
    origins: MemoryRetrievalOrigins
