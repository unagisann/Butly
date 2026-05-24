"""Pydantic model for per-instance config.json.

The full schema lands in a later phase; Phase 1 only provides an importable
placeholder that accepts the existing loose shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InstanceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
