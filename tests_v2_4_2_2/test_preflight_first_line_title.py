from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline_v2.title_hotfix_v2422 import (
    first_line_special_plan_evidence,
    recover_full_document_title_v2422,
)


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "cleaned_v2_4_2_2" / "pilot_100_results_v2_4_2_2.json"


@pytest.fixture(scope="module")
def rows() -> dict[str, dict]:
    if not RESULTS.exists():
        pytest.skip("requires private pilot output")
    return {row["document_id"]: row for row in json.loads(RESULTS.read_text(encoding="utf-8"))}


def _record(first_line: str, body: str) -> dict:
    return {
        "document_id": "synthetic-preflight",
        "parent_document_type": "enterprise_plan",
        "segment_type": "full_document",
        "accident_category": "chemical_leakage",
        "content_role": "plan_structure_reference",
        "review_status": "pending",
        "title": "",
        "title_status": "missing",
        "title_source": "missing",
        "original_title": "",
        "source_file": "/tmp/opaque.pdf",
        "raw_text": f"{first_line}\n{body}",
        "sections": [],
    }


def test_real_special_plan_title_with_chinese_sequence_is_not_cleared():
    record = _record(
        "二 危险化学品泄漏事故专项应急预案",
        "事故风险分析：储罐可能泄漏。\n应急指挥部负责组织救援。\n应急响应启动后实施堵漏和警戒处置。",
    )
    decision = recover_full_document_title_v2422(record)
    assert decision["title"] == "二 危险化学品泄漏事故专项应急预案"
    assert decision["status"] == "valid"
    assert decision["source"] == "raw_text_document_title"


def test_ordinary_chapter_still_cannot_be_full_document_title():
    record = _record(
        "第一节 一般规定",
        "事故风险分析。\n应急指挥部负责组织救援。\n应急响应和应急处置。",
    )
    assert first_line_special_plan_evidence(record) is None


def test_toc_dotted_special_plan_line_still_cannot_be_document_title():
    record = _record(
        "二 危险化学品泄漏事故专项应急预案 ........ 38",
        "事故风险分析。\n应急指挥部负责组织救援。\n应急响应和应急处置。",
    )
    assert first_line_special_plan_evidence(record) is None


@pytest.mark.parametrize(
    ("document_id", "expected_title"),
    [
        ("rec-68a0295ecf0bdd54", "五 自然灾害专项应急预案"),
        ("rec-54e0cabba9e4350d", "六 建设工程安全生产事故专项应急预案"),
    ],
)
def test_preflight_regression_records_restore_without_classification_change(rows, document_id, expected_title):
    frozen = ("parent_document_type", "segment_type", "accident_category", "content_role")
    record = copy.deepcopy(rows[document_id])
    before = {field: record.get(field) for field in frozen}
    decision = recover_full_document_title_v2422(record)
    after = {field: record.get(field) for field in frozen}
    assert decision["title"] == expected_title
    assert decision["status"] == "valid"
    assert before == after


def test_preflight_results_remain_pending(rows):
    assert all(row.get("review_status") == "pending" for row in rows.values())
