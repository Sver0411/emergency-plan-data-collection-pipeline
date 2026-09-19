from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

ReviewStatus = Literal["pending", "approved", "rejected"]
PrimaryType = Literal["enterprise_special_plan", "organization_special_plan", "government_or_regional_plan", "onsite_disposal_plan", "embedded_special_section", "sds", "chemical_catalog", "guidance_reference", "regulation", "accident_case", "reject", "manual_review"]
ParentDocumentType = Literal["enterprise_plan", "enterprise_group_plan", "government_or_regional_plan", "organization_plan", "sds", "chemical_catalog", "regulation", "guidance_reference", "accident_case", "other", "reject", "manual_review"]
SegmentType = Literal["full_document", "embedded_special_section", "onsite_disposal_plan", "appendix", "appendix_sds", "embedded_sds", "table", "toc_fragment", "guidance_section", "accident_case_section", "list_fragment", "reference_sentence", "other"]

class FileRegistryRecord(BaseModel):
    document_id: str = Field(min_length=8)
    source_file: str
    source_url: str = ""
    file_name: str
    file_type: str
    file_size: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duplicate_of: str | None = None
    parse_status: str = "pending"
    review_status: ReviewStatus = "pending"

class DocumentBlock(BaseModel):
    block_id: str
    block_type: str
    text: str
    page: int | None = None
    level: int | None = None

class ParsedDocument(BaseModel):
    document_id: str
    parser: str
    parser_version: str = ""
    pages: list[dict] = []
    blocks: list[DocumentBlock] = []
    raw_text: str = ""
    warnings: list[str] = []
    parse_quality: dict = {}
    review_status: ReviewStatus = "pending"

class PlanSection(BaseModel):
    section_id: str
    heading: str
    content: str
    start_page: int | None = None
    end_page: int | None = None
    start_block_id: str = ""
    end_block_id: str = ""
    topic: str = ""
    boundary_confidence: float = 0.0
    warnings: list[str] = []
    start_position: int = 0
    end_position: int = 0
    @model_validator(mode="after")
    def valid_range(self):
        if self.end_position < self.start_position:
            raise ValueError("section end precedes start")
        return self

class ClassifiedDocument(BaseModel):
    document_id: str
    primary_type: PrimaryType
    category: str = "other"
    classification_reasons: list[str] = []
    classification_evidence: list[str] = []
    classification_confidence: float = 0.0
    requires_manual_review: bool = False
    review_status: ReviewStatus = "pending"

class ClassifiedDocumentV22(BaseModel):
    """V2.2 keeps parent identity separate from the role of an extracted segment."""
    document_id: str
    parent_document_type: ParentDocumentType
    segment_type: SegmentType
    accident_category: str = "other"
    content_role: str = "manual_review"
    classification_reasons: list[str] = []
    classification_evidence: list[dict] = []
    classification_confidence: float = Field(ge=0.0, le=1.0)
    requires_manual_review: bool = True
    review_status: ReviewStatus = "pending"

    @model_validator(mode="after")
    def calibrated_confidence(self):
        strong = [item for item in self.classification_evidence if item.get("strength") == "strong"]
        if not self.classification_evidence and self.classification_confidence > 0.3:
            raise ValueError("records without evidence cannot exceed confidence 0.3")
        if self.classification_confidence >= 0.8 and len(strong) < 2:
            raise ValueError("confidence >= 0.8 requires two independent strong evidence items")
        if self.classification_confidence >= 0.9:
            if any(not item.get("text") or item.get("start") is None or item.get("end") is None for item in strong):
                raise ValueError("confidence >= 0.9 requires traceable strong evidence")
        if self.classification_confidence >= 0.8 and self.requires_manual_review and self.parent_document_type != "manual_review":
            # High-confidence records may still be sampled for review, but they are not forced
            # into the uncertainty queue solely by this model.
            pass
        return self

class EnterprisePlan(BaseModel):
    document_id: str
    title: str
    primary_type: PrimaryType
    category: str
    plan_level: str
    sections: list[PlanSection]
    source_url: str
    review_status: ReviewStatus = "pending"
    @model_validator(mode="after")
    def core_evidence(self):
        if self.primary_type == "enterprise_special_plan" and not self.sections:
            raise ValueError("enterprise plan needs sections")
        return self

class SDSRecord(BaseModel):
    document_id: str
    chemical_name_cn: str = ""
    chemical_name_en: str = ""
    cas: str = ""
    product_form: str = ""
    concentration: str = ""
    is_mixture: bool | None = None
    components: list[str] = []
    supplier: str = ""
    revision_date: str = ""
    hazards: list[dict] = []
    spill_response: list[dict] = []
    ppe: list[dict] = []
    first_aid: dict[str, list[dict]] = {"inhalation": [], "skin": [], "eyes": [], "ingestion": []}
    incompatible_materials: list[dict] = []
    environmental_precautions: list[dict] = []
    forbidden_actions: list[dict] = []
    source_urls: list[str] = []
    source_sections: list[str] = []
    conflict_notes: list[str] = []
    review_status: ReviewStatus = "pending"
    @field_validator("cas")
    @classmethod
    def cas_format(cls, value: str):
        if value and not re.fullmatch(r"\d{2,7}-\d{2}-\d", value):
            raise ValueError("invalid CAS format")
        return value

class KnowledgeItem(BaseModel):
    kind: str
    text: str
    document_id: str
    source_url: str
    source_section: str
    start_position: int
    end_position: int
    flags: list[str] = []
    review_status: ReviewStatus = "pending"

class DuplicateGroup(BaseModel):
    group_id: str
    document_ids: list[str]
    classification: str
    metrics: dict
    review_status: ReviewStatus = "pending"

class RedactionAudit(BaseModel):
    document_id: str
    entity_type: str
    original_hash: str
    replacement: str
    start: int
    end: int
    recognizer: str
    score: float
    review_status: ReviewStatus = "pending"

class ManualReviewRecord(BaseModel):
    document_id: str
    reason: str
    stage: str
    source_url: str
    review_status: ReviewStatus = "pending"
