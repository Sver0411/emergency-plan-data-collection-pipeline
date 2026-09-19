from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
import pytest

from pipeline_v2.classification_v241 import sentence_title_reasons
from pipeline_v2.title_audit_v241 import audit_title_consistency_v241


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "cleaned_v2_4_1" / "pilot_100_results_v2_4_1.json").exists():
    pytest.skip("requires private pilot output", allow_module_level=True)
RESULTS = json.loads((ROOT / "cleaned_v2_4_1" / "pilot_100_results_v2_4_1.json").read_text(encoding="utf-8"))
BY_ID = {item["document_id"]: item for item in RESULTS}


def test_v241_parent_regression_targets():
    expected = {
        "file-4bdbb2104805d258": ("government_or_regional_plan", None),
        "file-45fdada3125c59db": ("government_or_regional_plan", None),
        "file-64c2f85b0479e145": ("guidance_reference", "emergency_preparedness_guidance"),
        "file-a08760e95f4acd0d": ("manual_review", None),
    }
    for parent, (parent_type, role) in expected.items():
        records = [item for item in RESULTS if item["parent_document_id"] == parent]
        assert records
        assert {item["parent_document_type"] for item in records} == {parent_type}
        if role:
            assert {item["content_role"] for item in records} == {role}


def test_v241_child_segment_regression_targets():
    for ident in ("rec-a36e466db102df32", "rec-f1a05239ae5db834"):
        item = BY_ID[ident]
        assert item["segment_type"] == "accident_case_section"
        assert item["accident_category"] == "poisoning_asphyxia"
    fragment = BY_ID["rec-b46cc6d5bec686b8"]
    assert fragment["segment_type"] == "list_fragment"
    assert fragment["segment_type"] != "embedded_special_section"


def test_v241_reference_and_all_segment_types_are_reenabled():
    reference = BY_ID["rec-090f60df96164dc6"]
    assert reference["segment_type"] == "reference_sentence"
    allowed = {
        "accident_case_section", "reference_sentence", "list_fragment", "appendix_sds",
        "embedded_sds", "guidance_section", "onsite_disposal_plan",
        "embedded_special_section", "toc_fragment", "table", "full_document",
    }
    assert {item["segment_type"] for item in RESULTS} <= allowed


def test_v241_legal_nominal_titles_are_not_sentence_warnings():
    legal = [
        "第一部分综合应急预案",
        "有限空间事故应急预案",
        "危险化学品事故专项应急预案",
        "天然气保供突发事件专项应急预案",
        "生产安全事故专项应急预案",
    ]
    for title in legal:
        assert sentence_title_reasons(title, has_heading_evidence=True) == []
    for item in RESULTS:
        if item["title_status"] == "valid":
            assert "sentence_used_as_title" not in item["warnings"]


def test_v241_invalid_titles_need_two_sentence_signals():
    assert sentence_title_reasons("应急组织机构", has_heading_evidence=False) == []
    assert len(sentence_title_reasons("组织相关人员核实事故情况……", has_heading_evidence=False)) >= 2
    assert len(sentence_title_reasons("负责制定和实施应急处置措施；", has_heading_evidence=False)) >= 2
    assert len(sentence_title_reasons("48小时以上", has_heading_evidence=False)) >= 2


def test_v241_unrecoverable_title_is_retained_and_uncertain():
    item = BY_ID["rec-9fa62ab55d92f636"]
    assert item["title"] == item["original_title"]
    assert item["title_status"] == "uncertain"
    assert item["requires_manual_review"] is True


def test_v241_title_consistency_states_cover_required_cases():
    inconsistent = audit_title_consistency_v241(
        document_id="test-one",
        title="危险化学品泄漏事故专项应急预案",
        text="9、有限空间作业事故专项应急预案\n本节说明有限空间缺氧事故。",
        segment_type="embedded_special_section",
        title_status="valid",
        title_source="body_heading",
        sections=[{"heading": "9、有限空间作业事故专项应急预案"}],
    )
    uncertain = audit_title_consistency_v241(
        document_id="test-two",
        title="速按照本综合应急预案",
        text="执行相关预案。",
        segment_type="reference_sentence",
        title_status="uncertain",
        title_source="source_record",
        sections=[],
    )
    assert inconsistent["status"] == "inconsistent"
    assert uncertain["status"] == "uncertain"
    assert {item["title_consistency"]["status"] for item in RESULTS} <= {
        "consistent", "inconsistent", "uncertain", "not_applicable"
    }


def test_v241_same_locked_100_and_all_pending():
    manifest = json.loads((ROOT / "reports_v2" / "pilot_sample_manifest_locked_v2_1.json").read_text(encoding="utf-8"))
    base = [item for item in RESULTS if not item["document_id"].endswith("-appendix-sds")]
    assert len(manifest["records"]) == len(base) == 100
    assert len(RESULTS) == 101

    bad = []

    def walk(value):
        if isinstance(value, dict):
            if "review_status" in value and value["review_status"] != "pending":
                bad.append(value["review_status"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(RESULTS)
    assert bad == []


def test_v241_required_reports_exist():
    names = [
        "pilot_100_report_v2_4_1.md",
        "manual_review_all_parents_49_v2_4_1.md",
        "manual_review_changed_parents_v2_4_1.md",
        "manual_review_accident_case_sections_v2_4_1.md",
        "manual_review_reference_and_list_fragments_v2_4_1.md",
        "manual_review_segments_30_v2_4_1.md",
        "title_recovery_audit_v2_4_1.md",
        "title_body_consistency_v2_4_1.md",
        "segment_type_changes_v2_4_1.md",
        "regression_test_report_v2_4_1.md",
    ]
    for name in names:
        assert (ROOT / "reports_v2_4_1" / name).exists()


def test_v241_production_has_no_record_id_answers():
    from pipeline_v2 import classification_v241, pilot_runner_v2_4_1, title_audit_v241

    source = "\n".join(inspect.getsource(module) for module in (classification_v241, pilot_runner_v2_4_1, title_audit_v241))
    assert not re.search(r"(?:rec|file)-[0-9a-f]{16}", source)
