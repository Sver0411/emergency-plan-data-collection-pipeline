"""Strict Pydantic contracts for the first full-run repair."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Pending = Literal["pending"]


class RepairBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class RepairedParent(RepairBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    source_file: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    parent_document_type: str = Field(min_length=1)
    parent_title: str = ""
    parent_title_status: str = Field(pattern=r"^(valid|uncertain|missing)$")
    parent_title_source: str
    parse_status: str
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def parent_ids_match(self):
        if self.document_id != self.parent_document_id:
            raise ValueError("file-level parent ids must match")
        return self


class RepairedSegment(RepairBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    canonical_segment_id: str = Field(min_length=8)
    normalized_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_type: str = Field(min_length=1)
    parent_title: str
    section_title: str = ""
    content: str
    source_file: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def positions_are_valid(self):
        if self.char_end < self.char_start:
            raise ValueError("segment char_end precedes char_start")
        return self


class RepairedChunk(RepairBase):
    chunk_id: str = Field(min_length=8)
    canonical_segment_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    parent_title: str = Field(min_length=1)
    display_title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    normalized_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def chunk_positions_are_valid(self):
        if self.char_end < self.char_start:
            raise ValueError("chunk char_end precedes char_start")
        return self


class RepairValidation(RepairBase):
    model_name: str
    checked: int = Field(ge=0)
    passed: int = Field(ge=0)
    failures: list[dict[str, Any]] = []
