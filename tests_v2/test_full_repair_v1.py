"""Regression tests for first full-run repair rules."""
from __future__ import annotations

from pathlib import Path

from pipeline_v2.models_repair_v1 import RepairedChunk, RepairedParent
from pipeline_v2.repair_full_v1 import canonicalize_segments, make_segment_row, raw_unchanged
from pipeline_v2.repair_rules_v1 import (
    accident_category_repaired,
    classify_parent_repaired,
    data_quality_reasons,
    is_system_file,
    parse_html_local_repaired,
    professional_review_reasons,
    segment_is_rag_eligible,
)


def classify(text: str, title: str = "文档") -> str:
    return classify_parent_repaired(text=text, title=title, source_file="sample.pdf")["parent_document_type"]


def sds_text() -> str:
    return "\n".join([
        "化学品安全技术说明书（SDS）", "化学品名称：氯气", "供应商：某某有限公司",
        "CAS号：7782-50-5", "危险性概述", "成分/组成信息", "急救措施",
        "消防措施", "泄漏应急处理", "操作处置与储存", "接触控制/个体防护",
        "本说明书提供产品危害和安全使用信息。" * 8,
    ])


def parent_stub(**overrides):
    row = {
        "document_id": "file-12345678", "parent_document_id": "file-12345678",
        "source_file": "/tmp/a.txt", "relative_path": "a.txt", "sha256": "a" * 64,
        "parent_document_type": "guidance_reference", "parent_title": "安全指导资料",
        "parent_title_status": "valid", "parent_title_source": "cover",
        "parse_status": "success", "accident_category": "other", "secondary_topic": "",
        "content_role": "guidance_reference", "warnings": [], "requires_manual_review": False,
        "quality_grade": "gold_candidate", "review_status": "pending", "source_url": "https://example.test/a",
    }
    row.update(overrides)
    return row


def test_sds_supplier_name_does_not_trigger_enterprise_plan():
    assert classify(sds_text(), "氯气化学品安全技术说明书") == "sds"


def test_complete_sds_is_classified_as_sds():
    assert classify(sds_text(), "氯气安全数据表") == "sds"


def test_legacy_safety_technical_sheet_is_still_sds():
    text = "氯气安全技术说明书\n化学品名称：氯气\nCAS号：7782-50-5\n危险性概述\n急救措施\n消防措施\n泄漏应急处理\n操作和储存\n" + "安全使用资料。" * 30
    assert classify(text, "氯气安全技术说明书") == "sds"


def test_accident_investigation_report_is_not_enterprise_plan():
    text = "2024年3月20日，山东吉田生物有限公司发生事故。事故经过如下。造成2人死亡1人受伤。事故原因分析。责任追究。整改措施。" * 5
    assert classify(text, "山东吉田生物3·20事故调查报告") == "accident_case"


def test_street_office_plan_is_government_plan():
    text = "临港街道办事处编制。本预案适用于本街道辖区。成员单位职责和区域资源调度。事故风险分析。应急组织机构与职责。应急响应。应急处置。" * 4
    assert classify(text, "临港街道办事处企业生产安全事故专项应急预案") == "government_or_regional_plan"


def test_provincial_plan_is_not_enterprise_group_plan():
    text = "山东省人民政府发布，适用于全省辖区。成员单位职责。事故风险分析。应急组织。应急响应和处置。" * 5
    assert classify(text, "山东省危险化学品事故应急预案") == "government_or_regional_plan"


def test_quoted_laws_do_not_turn_accident_case_into_regulation():
    text = "2023年6月2日某项目发生高处坠落事故。事故经过。造成1人死亡。事故原因。责任追究。整改措施。调查组依据《安全生产法》《工贸企业有限空间作业安全规定》开展调查。" * 5
    assert classify(text, "某项目6·2事故调查报告") == "accident_case"


def test_emergency_plan_management_measure_is_regulation():
    text = "生产安全事故应急预案管理办法\n第一条 为规范应急预案管理。\n第二条 本办法适用于预案编制。\n第三条 应急管理部门负责监督。\n第四条 生产经营单位依法执行。" * 4
    assert classify(text, "生产安全事故应急预案管理办法") == "regulation"


def test_government_information_disclosure_guide_is_excluded():
    text = "政府信息公开指南。依申请公开流程、办公地址、联系电话、网站导航和用户登录说明。" * 8
    assert classify(text, "某区政府信息公开指南") == "excluded_irrelevant"


def test_work_at_height_is_not_confined_space():
    category, secondary = accident_category_repaired("高处坠落事故整改评估报告", "高处作业人员从脚手架坠落。", "accident_case")
    assert (category, secondary) == ("other", "fall_from_height")


def test_ds_store_never_generates_parent_document():
    result = classify_parent_repaired(text="", title="", source_file="/tmp/.DS_Store", parse_status="failed")
    assert is_system_file(".DS_Store") and result["parent_document_type"] is None


def test_parent_document_object_is_not_direct_rag_input():
    parent = parent_stub()
    assert segment_is_rag_eligible(parent, parent) is False


def test_same_rec_and_seg_only_one_is_canonical():
    base = {"parent_document_id": "file-12345678", "normalized_content_hash": "b" * 64, "content": "相同正文" * 100, "is_canonical": True, "canonical_segment_id": "", "duplicate_segment_ids": [], "duplicate_reason": ""}
    rows = [
        {**base, "document_id": "seg-12345678", "canonical_segment_id": "seg-12345678", "source_origin": "parent_parsed_text"},
        {**base, "document_id": "rec-12345678", "canonical_segment_id": "rec-12345678", "source_origin": "source_record_repaired", "duplicate_segment_ids": []},
    ]
    repaired, relations = canonicalize_segments(rows)
    assert sum(row["is_canonical"] for row in repaired) == 1
    assert len(relations) == 1


def test_segment_without_own_title_inherits_parent_title():
    parent = parent_stub()
    segment = make_segment_row(parent, segment_id="seg-12345678", text="安全指导正文。" * 50, section_title="", segment_type="full_document", start=0, end=350, origin="parent_parsed_text", is_parent_full=True)
    assert segment["parent_title"] == "安全指导资料"
    assert segment["display_title"] == "安全指导资料"


def test_professional_content_warning_does_not_create_data_quality_reason():
    record = parent_stub()
    assert professional_review_reasons("发生泄漏后立即堵漏并佩戴呼吸器")
    assert data_quality_reasons(record) == []


def test_fallback_html_is_reclassified_from_recovered_body():
    raw = b"<html><head><title>Government</title></head><body><h1>Test emergency plan</h1><p>" + (b"substantive emergency response text " * 20) + b"</p></body></html>"
    parsed = parse_html_local_repaired(raw)
    assert parsed["parse_status"] == "success"
    assert parsed["text_length"] >= 120


def test_parse_failed_record_cannot_enter_rag():
    parent = parent_stub(parse_status="failed")
    segment = {"review_status": "pending", "is_canonical": True, "segment_type": "full_document", "content": "正文" * 100}
    assert segment_is_rag_eligible(segment, parent) is False


def test_excluded_irrelevant_cannot_enter_rag():
    parent = parent_stub(parent_document_type="excluded_irrelevant")
    segment = {"review_status": "pending", "is_canonical": True, "segment_type": "full_document", "content": "正文" * 100}
    assert segment_is_rag_eligible(segment, parent) is False


def test_repaired_models_require_pending_status():
    RepairedParent.model_validate(parent_stub())
    try:
        RepairedParent.model_validate(parent_stub(review_status="approved"))
    except Exception:
        pass
    else:
        raise AssertionError("approved must not validate")


def test_chunk_contract_contains_no_embedding_vector():
    chunk = RepairedChunk.model_validate({
        "chunk_id": "chunk-12345678", "canonical_segment_id": "seg-12345678",
        "parent_document_id": "file-12345678", "parent_title": "标题", "display_title": "标题",
        "content": "正文", "normalized_content_hash": "a" * 64, "source_file": "/tmp/a.txt",
        "char_start": 0, "char_end": 2, "review_status": "pending",
    })
    assert "embedding" not in chunk.model_dump()


def test_repair_rules_do_not_contain_database_import_operation():
    source = Path("pipeline_v2/repair_full_v1.py").read_text(encoding="utf-8")
    assert "INSERT INTO" not in source.upper()
    assert "create_engine(" not in source


def test_raw_sha256_comparison_reports_unchanged(tmp_path: Path):
    path = tmp_path / "raw.txt"
    path.write_text("原始资料", encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ok, changes, checked = raw_unchanged([{"source_file": str(path), "sha256": digest}])
    assert ok and changes == [] and checked == 1
