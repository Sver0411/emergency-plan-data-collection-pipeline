"""Pydantic contracts for the title/exclusion fixed full corpus."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Pending = Literal["pending"]


class TitleFixedBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class TitleFixedParent(TitleFixedBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    source_file: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    parent_document_type: str = Field(min_length=1)
    parent_title: str = ""
    parent_title_status: Literal["valid", "uncertain", "missing"]
    parent_title_source: str = Field(min_length=1)
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def parent_ids_match(self):
        if self.document_id != self.parent_document_id:
            raise ValueError("file-level parent ids must match")
        return self


class TitleFixedSegment(TitleFixedBase):
    document_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    canonical_segment_id: str = Field(min_length=8)
    normalized_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_document_type: str = Field(min_length=1)
    segment_type: str = Field(min_length=1)
    parent_title: str
    section_title: str | None = None
    section_title_source: str = Field(min_length=1)
    section_title_confidence: float = Field(ge=0.0, le=1.0)
    section_title_valid: bool
    # A non-RAG manual-review segment may inherit an unresolved parent title.
    # RAG chunks remain stricter and always require a display title.
    display_title: str = ""
    content: str
    source_file: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def positions_and_title_are_valid(self):
        if self.char_end < self.char_start:
            raise ValueError("segment char_end precedes char_start")
        if self.section_title_valid != bool(self.section_title):
            raise ValueError("section_title_valid must match section_title presence")
        return self


class TitleFixedChunk(TitleFixedBase):
    chunk_id: str = Field(min_length=8)
    canonical_segment_id: str = Field(min_length=8)
    parent_document_id: str = Field(min_length=8)
    parent_title: str = Field(min_length=1)
    section_title: str | None = None
    section_title_source: str = Field(min_length=1)
    section_title_confidence: float = Field(ge=0.0, le=1.0)
    section_title_valid: bool
    display_title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    normalized_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    review_status: Pending = "pending"

    @model_validator(mode="after")
    def positions_and_title_are_valid(self):
        if self.char_end < self.char_start:
            raise ValueError("chunk char_end precedes char_start")
        if self.section_title_valid != bool(self.section_title):
            raise ValueError("section_title_valid must match section_title presence")
        return self


class TitleFixValidation(TitleFixedBase):
    model_name: str
    checked: int = Field(ge=0)
    passed: int = Field(ge=0)
    failures: list[dict[str, Any]] = []
