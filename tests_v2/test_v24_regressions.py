from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path
import pytest

from pipeline_v2.classification_v24 import (
    first_effective_title,
    is_sentence_title,
    lock_parent_type,
    segment_type_v24,
)
from pipeline_v2.section_splitter_v24 import split_sections_v24
from pipeline_v2.pilot_runner_v2_4 import title_body_warning


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "json" / "records.json").exists() or not (ROOT / "cleaned_v2_4" / "pilot_100_results.json").exists():
    pytest.skip("requires private source records and pilot output", allow_module_level=True)
ROWS = json.loads((ROOT / "json" / "records.json").read_text(encoding="utf-8"))
V24 = json.loads((ROOT / "cleaned_v2_4" / "pilot_100_results.json").read_text(encoding="utf-8"))
BY_ID = {item["document_id"]: item for item in V24}


def parent_id(row: dict) -> str:
    identity = row.get("file") or row.get("url") or row.get("title") or "missing-parent"
    return "file-" + hashlib.sha256(str(identity).encode()).hexdigest()[:16]


def record_id(index: int, row: dict) -> str:
    seed = f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


BY_SOURCE_ID = {record_id(index, row): row for index, row in enumerate(ROWS)}


def _parent_lock_for(parent: str):
    row = next(row for row in ROWS if parent_id(row) == parent)
    parsed = ROOT / "cleaned_v2" / "parsed_documents" / f"{parent}.json"
    text = json.loads(parsed.read_text(encoding="utf-8")).get("raw_text", "") if parsed.exists() else row.get("content", "")
    return lock_parent_type(parent, row.get("title", ""), text, row.get("type", ""))


def test_v24_parent_lock_uses_cover_and_keeps_plan_parents():
    expected = {
        "file-7b0533b3a9003d07": "enterprise_plan",
        "file-d12533f3db0f6fcb": "enterprise_plan",
        "file-683887c4be3b398a": "enterprise_plan",
        "file-ae070816763c32a6": "enterprise_plan",
        "file-17b68c95b53682dd": "sds",
    }
    for parent, parent_type in expected.items():
        lock = _parent_lock_for(parent)
        assert lock.result.parent_document_type == parent_type
        assert lock.result.review_status == "pending"


def test_v24_sds_attachment_does_not_override_parent():
    for ident in (
        "rec-79243fded2d7edb1",
        "rec-2f3a381fef572183",
        "rec-30bf2027d5eb18d7",
        "rec-67a5da6cb0166847",
    ):
        item = BY_ID[ident]
        assert item["parent_document_type"] == "enterprise_plan"
        assert item["parent_type_locked"] is True
        assert item["parent_document_type"] != "sds"

    standalone = BY_ID["rec-0dfd57dbafa55da8"]
    assert standalone["parent_document_type"] == "sds"
    assert standalone["segment_type"] == "full_document"


def test_v24_child_types_categories_and_title_recovery():
    expected = {
        "rec-79243fded2d7edb1": ("embedded_special_section", "chemical_leakage"),
        "rec-2f3a381fef572183": ("onsite_disposal_plan", "other"),
        "rec-30bf2027d5eb18d7": ("embedded_special_section", "fire_explosion"),
        "rec-67a5da6cb0166847": ("embedded_special_section", {"major_hazard", "multi_hazard"}),
        "rec-4c900dd12d4bf6e1": ("embedded_special_section", "confined_space"),
        "rec-5bb306aaf7cbe8ad": ("onsite_disposal_plan", "confined_space"),
        "rec-4f7dba565e986872": ("embedded_special_section", "natural_disaster"),
    }
    for ident, (segment_type, category) in expected.items():
        item = BY_ID[ident]
        assert item["segment_type"] == segment_type
        assert item["accident_category"] in category if isinstance(category, set) else item["accident_category"] == category
        assert item["review_status"] == "pending"

    assert "采取桌面演练" not in BY_ID["rec-b3696b19a955dd2a"]["title"]
    assert "专项应急预案" in BY_ID["rec-b3696b19a955dd2a"]["title"]
    assert "有限空间" in BY_ID["rec-9fa62ab55d92f636"]["title"]
    assert not is_sentence_title(BY_ID["rec-9fa62ab55d92f636"]["title"])
    assert "结合本单位" not in BY_ID["rec-5bb306aaf7cbe8ad"]["title"]
    assert "天然气保供" in BY_ID["rec-04eba8f63269599e"]["title"]
    assert not BY_ID["rec-04eba8f63269599e"]["title"].startswith(("（4）", "(4)"))
    assert "上海市" in BY_ID["rec-f84100a487ae4497"]["title"]
    assert not BY_ID["rec-f84100a487ae4497"]["title"].startswith(("（14）", "(14)"))


def test_v24_fake_heading_rules_and_boundaries():
    text = """1 总则
有效正文。
2 0.022 50 0.0004
表格数值。
3 总指挥：张三
人员字段。
4 34号）
法规文号残片。
5 48小时以上
时间参数。
6 负责组织人员立即撤离现场并关闭阀门。
处置句正文。
7 有限空间事故专项应急预案
有限空间正文。
8 新现场处置方案
现场处置正文。
9 丁小建 机电一体化技术 南通骏如安全技术服务有限公司 注册安全工程师 13222168905
人员表格行。
"""
    headings = [section.heading for section in split_sections_v24(text, "confined_space")]
    assert "1 总则" in headings
    assert all(token not in "\n".join(headings) for token in ("0.022", "总指挥", "34号", "48小时", "负责组织"))
    assert "7 有限空间事故专项应急预案" in headings
    assert "8 新现场处置方案" in headings
    assert all("丁小建" not in heading for heading in headings)


def test_v24_parent_lock_is_before_child_and_no_id_answers_in_production():
    source = inspect.getsource(__import__("pipeline_v2.classification_v24", fromlist=["*"]))
    runner_source = inspect.getsource(__import__("pipeline_v2.pilot_runner_v2_4", fromlist=["*"]))
    assert not re.search(r"(?:rec|file)-[0-9a-f]{16}", source)
    assert not re.search(r"(?:rec|file)-[0-9a-f]{16}", runner_source)
    assert all(item["parent_type_locked"] is True for item in V24)
    assert all(item["review_status"] == "pending" for item in V24)
    assert all(item.get("sds") is None or item["sds"]["review_status"] == "pending" for item in V24)


def test_v24_title_detection_ignores_sds_attachment_words_for_plan_parent():
    row = BY_SOURCE_ID["rec-79243fded2d7edb1"]
    parent = parent_id(row)
    parsed = ROOT / "cleaned_v2" / "parsed_documents" / f"{parent}.json"
    text = json.loads(parsed.read_text(encoding="utf-8")).get("raw_text", "")
    lock = lock_parent_type(parent, row.get("title", ""), text, row.get("type", ""))
    assert lock.result.parent_document_type == "enterprise_plan"
    assert "appendix_sds" in lock.embedded_content_types


def test_v24_title_body_audit_uses_body_not_repeated_title():
    compatible = "危险化学品泄漏事故专项应急预案\n一、事故风险分析\n本单位涉及盐酸、氨水等危险化学品，CAS号可追溯。"
    contaminated = "危险化学品泄漏事故专项应急预案\n9、受限空间作业事故专项应急预案\n本节仅说明有限空间缺氧风险。"
    assert title_body_warning("危险化学品泄漏事故专项应急预案", compatible) is False
    assert title_body_warning("危险化学品泄漏事故专项应急预案", contaminated) is True


def test_v24_required_review_packages_and_metrics_exist():
    required = [
        "manual_review_all_parents_49.json",
        "manual_review_changed_parents.json",
        "manual_review_enterprise_parents.json",
        "manual_review_sds_parents.json",
        "manual_review_high_risk.json",
        "manual_review_segment_30.json",
        "repair_metrics_v2_4.json",
    ]
    for name in required:
        assert (ROOT / "reports_v2_4" / name).exists()

    changed = json.loads((ROOT / "reports_v2_4" / "manual_review_changed_parents.json").read_text(encoding="utf-8"))
    assert all(item["previous_parent_document_type"] != item["parent_document_type"] for item in changed)
    assert len({item["parent_document_id"] for item in changed}) == len(changed)

    high_risk = json.loads((ROOT / "reports_v2_4" / "manual_review_high_risk.json").read_text(encoding="utf-8"))
    reasons = {reason for item in high_risk for reason in item.get("high_risk_reasons", [])}
    assert {
        "title_body_mismatch_corrected_candidate",
        "parent_contains_sds_attachment",
        "enterprise_and_government_evidence_coexist",
        "sentence_used_as_title",
        "cross_special_plan_contamination",
    } <= reasons

    metrics = json.loads((ROOT / "reports_v2_4" / "repair_metrics_v2_4.json").read_text(encoding="utf-8"))
    for key in (
        "enterprise_plan_count",
        "enterprise_group_plan_count",
        "standalone_sds_parent_count",
        "appendix_sds_count",
        "sds_attachment_overwrite_repaired_parent_count",
        "enterprise_misclassified_as_government_repaired_parent_count",
        "title_body_mismatch_count",
        "sentence_false_title_count",
    ):
        assert key in metrics
    assert metrics["review_status"] == "pending"
