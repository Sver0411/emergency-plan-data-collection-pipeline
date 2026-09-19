"""Pydantic contracts for the first production-sized cleaning run.

The full-run models deliberately keep ``extra='allow'`` because the stable
V2.4.2.2 pipeline already emits rich audit fields.  These contracts validate
the invariant fields without discarding the traceability payload.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReviewStatus = Literal["pending"]


class FullRunBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class FullManifestRecord(FullRunBase):
    document_id: str = Field(min_length=8)
    source_file: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    file_extension: str
    file_size: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_url: str = ""
    parse_status: str
    review_status: ReviewStatus = "pending"


class FullParentDocument(FullRunBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    parent_document_type: str = Field(min_length=1)
    parent_type_locked: bool
    source_file: str = Field(min_length=1)
    source_url: str = ""
    title: str = ""
    review_status: ReviewStatus = "pending"

    @model_validator(mode="after")
    def parent_identity_matches(self):
        if self.document_id != self.parent_document_id:
            raise ValueError("parent record document_id must equal parent_document_id")
        return self


class FullSegmentRecord(FullRunBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    parent_document_type: str = Field(min_length=1)
    segment_type: str = Field(min_length=1)
    accident_category: str = Field(min_length=1)
    content_role: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    title: str = ""
    title_status: str = Field(pattern=r"^(valid|uncertain|invalid|toc_only|missing)$")
    segment_start: int = Field(ge=0)
    segment_end: int = Field(ge=0)
    review_status: ReviewStatus = "pending"

    @model_validator(mode="after")
    def range_is_valid(self):
        if self.segment_end < self.segment_start:
            raise ValueError("segment_end precedes segment_start")
        return self


class FullRagChunk(FullRunBase):
    chunk_id: str = Field(min_length=8)
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    title: str
    content: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    review_status: ReviewStatus = "pending"

    @model_validator(mode="after")
    def chunk_range_is_valid(self):
        if self.char_end < self.char_start:
            raise ValueError("chunk char_end precedes char_start")
        return self


class ValidationSummary(FullRunBase):
    model_name: str
    checked: int = Field(ge=0)
    passed: int = Field(ge=0)
    failures: list[dict[str, Any]] = []
