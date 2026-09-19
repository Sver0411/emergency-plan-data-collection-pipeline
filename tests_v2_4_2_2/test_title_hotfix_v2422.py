from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline_v2.title_hotfix_v2422 import (
    audit_full_document_title_v2422,
    full_document_title_issues,
    recover_full_document_title_v2422,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "cleaned_v2_4_2_1" / "pilot_100_results_v2_4_2_1.json"


@pytest.fixture(scope="module")
def rows() -> dict[str, dict]:
    if not BASE.exists():
        pytest.skip("requires private pilot output")
    values = json.loads(BASE.read_text(encoding="utf-8"))
    return {item["document_id"]: item for item in values}


def _synthetic(**updates) -> dict:
    record = {
        "document_id": "synthetic-title-test",
        "parent_document_id": "synthetic-parent",
        "parent_document_type": "government_or_regional_plan",
        "segment_type": "full_document",
        "accident_category": "chemical_leakage",
        "content_role": "emergency_plan",
        "parent_type_locked": True,
        "review_status": "pending",
        "title": "2. 应急响应",
        "title_status": "valid",
        "title_source": "body_heading",
        "original_title": "某市危险化学品事故应急预案",
        "raw_text": "某市危险化学品事故应急预案\n1 总则\n2 应急响应\n事故发生后立即启动响应。",
        "source_file": "/tmp/某市危险化学品事故应急预案.pdf",
        "sections": [],
    }
    record.update(updates)
    return record


def test_full_document_title_recovery_runs_after_final_segment_hotfix():
    record = _synthetic()
    decision = recover_full_document_title_v2422(record)
    assert decision["title"] == "某市危险化学品事故应急预案"
    assert decision["title"] != record["title"]


def test_full_document_prefers_verified_parent_standard_title():
    record = _synthetic(
        title="某市危险化学品事故应急预案",
        title_source="attachment_title",
        raw_text="2. 应急响应\n发生事故后立即启动响应。",
    )
    decision = recover_full_document_title_v2422(record)
    assert decision["title"] == "某市危险化学品事故应急预案"
    assert decision["source"] == "parent_standard_title"


def test_sds_first_aid_sentence_cannot_be_full_document_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-43873dd8f43850ca"])
    assert decision["title"] == "硝酸 安全资料表（SDS）"
    assert not full_document_title_issues(decision["title"], "sds", decision["source"])


def test_sds_supplier_information_cannot_be_full_document_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-0dfd57dbafa55da8"])
    assert decision["title"] == "Custom Organic Standard 化学品安全技术说明书"
    assert "供应商" not in decision["title"]


def test_sds_section_heading_cannot_be_full_document_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-b625c23a1b7d98fb"])
    assert decision["title"] == "乙酸（醋酸）安全资料表（SDS）"
    assert "化學品與廠商資料" not in decision["title"]


def test_accident_bullet_intro_cannot_be_article_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-b1788c434c033391"])
    assert decision["title"] == "国务院安委办通报8起有限空间盲目施救导致伤亡扩大事故"
    assert decision["title"] != "8起事故分别是："


def test_legal_basis_item_cannot_be_plan_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-373f7291743d2674"])
    assert decision["title"] == "杨浦区处置危险化学品生产安全事故应急预案"
    assert not decision["title"].startswith("（14）")


@pytest.mark.parametrize(
    ("document_id", "expected_title"),
    [
        ("rec-0f77ba237feec4f2", "矿山救援规程"),
        ("rec-7401377f856c3cb7", "煤矿安全规程"),
    ],
)
def test_full_document_never_keeps_toc_only(rows, document_id, expected_title):
    decision = recover_full_document_title_v2422(rows[document_id])
    audit = audit_full_document_title_v2422(rows[document_id], decision)
    assert decision["title"] == expected_title
    assert audit["status"] != "toc_only"


def test_body_secondary_heading_cannot_override_document_title(rows):
    decision = recover_full_document_title_v2422(rows["rec-f55f679e4310e75f"])
    assert decision["title"] == "梅桥镇液氨事故应急预案"
    assert decision["title"] != "2. 应急响应"


def test_title_hotfix_does_not_mutate_frozen_classification_fields(rows):
    frozen = (
        "parent_document_type", "segment_type", "accident_category", "content_role",
        "parent_type_locked", "parent_type_evidence_scope", "embedded_content_types",
    )
    for base in rows.values():
        if base.get("segment_type") != "full_document":
            continue
        record = copy.deepcopy(base)
        before = {field: copy.deepcopy(record.get(field)) for field in frozen}
        recover_full_document_title_v2422(record)
        after = {field: record.get(field) for field in frozen}
        assert before == after


def test_all_fixed_sample_records_remain_pending(rows):
    assert rows
    assert all(item.get("review_status") == "pending" for item in rows.values())
