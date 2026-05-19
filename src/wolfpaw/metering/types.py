"""Shared dataclasses for the metering subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Per-call token tally, mirroring Anthropic's `usage` block."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model_id: str
    input_per_mtok_cents: int
    output_per_mtok_cents: int
    cache_read_per_mtok_cents: int
    cache_write_per_mtok_cents: int
    effective_from: datetime


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    """What `ModelClient.call` returns. `raw` is the underlying SDK response
    for callers that need the full structure (tool_use blocks, stop_reason, etc.)."""

    text: str
    usage: TokenCounts
    cost_cents: int
    model: str
    raw: Any = None


@dataclass(frozen=True, slots=True)
class PromptVersionRow:
    id: UUID
    agent: str
    version_label: str
    content_hash: str
    content_template: dict = field(default_factory=dict)
