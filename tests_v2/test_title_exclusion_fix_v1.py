"""Regressions for the final title and false-exclusion repair."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_v2.models_title_fixed_v1 import TitleFixedChunk, TitleFixedSegment
from pipeline_v2.title_fix_rules_v1 import (
    audit_section_title,
    extract_sds_chemical_precise,
    parent_title_issues,
    reclassify_excluded,
    recover_enterprise_title,
    recover_sds_title,
)


def sds_text(name_line: str = "化学品中文名称：氯气\n化学品英文名称：Chlorine") -> str:
    return "\n".join([
        "化学品安全技术说明书（SDS）", name_line, "CAS No. 7782-50-5",
        "化学品及企业标识", "危险性概述", "成分/组成信息", "急救措施", "消防措施",
        "泄漏应急处理", "操作处置与储存", "接触控制/个体防护", "理化特性",
        "稳定性和反应性", "毒理学信息", "生态学信息", "废弃处置", "运输信息", "法规信息",
        "安全正文。" * 100,
    ])


def excluded_parent(title: str, source: str = "/tmp/file.pdf", parse_status: str = "success") -> dict:
    return {
        "document_id": "file-12345678", "parent_document_id": "file-12345678",
        "parent_title": title, "canonical_title": title, "source_file": source,
        "parent_document_type": "excluded_irrelevant", "parse_status": parse_status,
    }


def test_sds_identity_stops_before_english_field():
    result = extract_sds_chemical_precise(sds_text(), "/tmp/chlorine.pdf")
    assert result["chemical_name"] == "氯气"
    assert "化学品英文名称" not in result["chemical_name"]


def test_sds_identity_removes_basis_and_identity_heading():
    text = """1 / 9
化学品安全技术说明书（SDS）
氢氧化钾
*依据GB/T17519-2013、GB/T16483-2008 编制
化学品及企业标识
产品中文名称 氢氧化钾
产品英文名称 Potassium Hydroxide
""" + sds_text("产品中文名称 氢氧化钾\n产品英文名称 Potassium Hydroxide")
    result = recover_sds_title(text, "/tmp/PDF 1 _ 9.pdf")
    assert result["title"] == "氢氧化钾 化学品安全技术说明书"


def test_sds_identity_does_not_use_section_or_supplier_as_chemical():
    text = "Safety Data Sheet\nHazards identification\nComposition\nFirst aid measures\nFire fighting measures\nAccidental release measures\nHandling and storage"
    result = extract_sds_chemical_precise(text, "/tmp/Microsoft Word - GHS_SDS_2.pdf")
    assert result["chemical_name"] == ""


def test_invalid_sds_parent_field_stickiness_is_detected():
    assert "field_or_page_stickiness" in parent_title_issues("氯气化学品英文名称 化学品安全技术说明书", "sds")
    assert not parent_title_issues("氯气 化学品安全技术说明书", "sds")


def test_enterprise_numeric_hash_and_generic_titles_are_invalid():
    for title in ("20240722041533505", "aqyjyba", "应急预案版本号", "突发环境事件应急预案", "本应急预案", "某企业应急预案材料", "某集团安全生产事故应急预案.docx"):
        assert parent_title_issues(title, "enterprise_plan")


def test_enterprise_title_recovers_company_and_formal_plan_name():
    row = {"parent_title": "20240722041533505", "parent_document_type": "enterprise_plan", "parent_title_source": "filename"}
    text = "华东化工有限公司\n突发环境事件应急预案\n编制目的\n适用范围\n事故风险分析\n组织机构与职责\n应急响应\n应急处置"
    result = recover_enterprise_title(row, text)
    assert result["title"] == "华东化工有限公司突发环境事件应急预案"
    assert result["status"] == "valid"


def test_enterprise_without_reliable_subject_remains_uncertain():
    row = {"parent_title": "aqyjyba", "parent_document_type": "enterprise_plan"}
    result = recover_enterprise_title(row, "编制目的\n风险分析\n应急响应\n应急处置")
    assert result["title"] == "企业生产安全事故应急预案"
    assert result["status"] == "uncertain"


def test_enterprise_directory_entry_cannot_override_cover_title():
    row = {"parent_title": "陕西榆林能源集团有限公司生产安全事故应急预案（下册）", "parent_document_type": "enterprise_group_plan", "parent_title_source": "cover"}
    text = "陕西榆林能源集团有限公司\n生产安全事故应急预案（下册）\n目录\n（二）发电厂全厂停电事故专项应急预案\n正文"
    result = recover_enterprise_title(row, text)
    assert result["title"] == "陕西榆林能源集团有限公司生产安全事故应急预案（下册）"


def test_excluded_complete_sds_is_restored_to_sds():
    result = reclassify_excluded(excluded_parent("Microsoft Word - SDS"), sds_text())
    assert result["parent_document_type"] == "sds"


def test_traditional_sds_with_compatibility_glyph_is_restored():
    text = "安全資料表\n一、化學品與廠商資料\n化學品名稱：醋酸\nCAS No.：64-19-7\n危害辨識\n急救措施\n滅火措施\n安全處置與儲存方法\n安定性及反應性\n廢棄處置方法\n" + "安全正文。" * 100
    result = reclassify_excluded(excluded_parent("Microsoft Word - GHS_SDS_2 - 國立臺灣大學"), text)
    assert result["parent_document_type"] == "sds"


def test_excluded_sds_template_without_specific_chemical_is_guidance():
    text = "\n".join([
        "物質安全資料表 SDS 教学模板", "化學品與廠商資料", "危害辨識", "組成/成分",
        "急救措施", "滅火措施", "洩漏處理方法", "安全處置與儲存方法", "暴露預防措施",
        "物理及化學性質", "安定性及反應性", "毒性資料", "生態資料", "廢棄處置方法", "運送資料", "法規資料",
        "本文件说明如何填写物质安全资料表。" * 20,
    ])
    result = reclassify_excluded(excluded_parent("GHS SDS 教学模板"), text)
    assert result["parent_document_type"] == "guidance_reference"
    assert result["content_role"] == "sds_guidance_or_template"


def test_excluded_accident_case_collection_is_restored():
    text = "2021年5月1日某公司发生事故。事故经过。造成2人死亡。事故原因分析。事故教训和防范措施。" * 20
    result = reclassify_excluded(excluded_parent("应急管理部公布一批有限空间作业生产安全事故典型案例", "/tmp/事故典型案例.html"), text)
    assert result["parent_document_type"] == "accident_case"
    assert result["content_role"] == "accident_case_collection"


def test_accident_collection_survives_ocr_missing_numeric_dates():
    text = "蔬菜腌制行业典型事故案例\n某食品有限公司较大中毒和窒息事故\n年 月 日，发生事故，造成人死亡。\n发生原因：吸入硫化氢。\n主要教训：未落实审批制度。" * 10
    result = reclassify_excluded(excluded_parent("工贸企业有限空间作业较大事故典型案例", "/tmp/cases.html"), text)
    assert result["parent_document_type"] == "accident_case"


def test_parse_failure_never_means_irrelevant():
    result = reclassify_excluded(excluded_parent("有限空间事故材料", parse_status="failed"), "")
    assert result["parent_document_type"] == "manual_review"


def segment(title: str, content: str, segment_type: str = "guidance_section", origin: str = "generated_structural_section") -> dict:
    return {"section_title": title, "content": content, "segment_type": segment_type, "source_origin": origin}


def test_date_narrative_cannot_be_section_title():
    title = "2024 年 9 月22 日，川丽德建设公司发生事故"
    result = audit_section_title(segment(title, title + "\n事故经过正文。"))
    assert result["section_title"] is None
    assert result["valid"] is False


def test_personnel_description_cannot_be_section_title():
    title = "1 人、党支部书记 1 人、工程技术管理人员"
    result = audit_section_title(segment(title, title + "\n后续正文。"))
    assert result["section_title"] is None


def test_action_sentence_cannot_be_section_title():
    title = "负责组织制定和实施事故应急预案"
    result = audit_section_title(segment(title, title + "\n后续正文。"))
    assert result["section_title"] is None


def test_real_heading_at_local_boundary_is_valid():
    result = audit_section_title(segment("5.3 救援注意事项", "5.3 救援注意事项\n一旦发生有限空间事故，应立即组织救援。"))
    assert result["section_title"] == "5.3 救援注意事项"
    assert result["valid"] is True


def test_title_not_at_boundary_is_cleared_instead_of_invented():
    result = audit_section_title(segment("应急响应", "事故风险分析\n正文内容。", origin="source_record_repaired"))
    assert result["section_title"] is None
    assert result["source"] == "rejected_non_heading_text"


def test_full_document_uses_parent_title_only():
    result = audit_section_title(segment("2 应急响应", "2 应急响应\n正文", "full_document"))
    assert result["section_title"] is None
    assert result["valid"] is False


def test_title_fixed_models_keep_nullable_section_and_pending():
    segment_payload = {
        "document_id": "seg-12345678", "parent_document_id": "file-12345678",
        "canonical_segment_id": "seg-12345678", "normalized_content_hash": "a" * 64,
        "parent_document_type": "guidance_reference", "segment_type": "full_document",
        "parent_title": "有限空间作业指南", "section_title": None,
        "section_title_source": "not_applicable_full_document", "section_title_confidence": 1.0,
        "section_title_valid": False, "display_title": "有限空间作业指南", "content": "正文",
        "source_file": "/tmp/a.pdf", "relative_path": "a.pdf", "char_start": 0, "char_end": 2,
        "review_status": "pending",
    }
    TitleFixedSegment.model_validate(segment_payload)
    chunk_payload = {
        "chunk_id": "chunk-12345678", "canonical_segment_id": "seg-12345678",
        "parent_document_id": "file-12345678", "parent_title": "有限空间作业指南",
        "section_title": None, "section_title_source": "not_applicable_full_document",
        "section_title_confidence": 1.0, "section_title_valid": False,
        "display_title": "有限空间作业指南", "content": "正文", "normalized_content_hash": "b" * 64,
        "source_file": "/tmp/a.pdf", "char_start": 0, "char_end": 2, "review_status": "pending",
    }
    TitleFixedChunk.model_validate(chunk_payload)


def test_generated_title_fixed_output_is_consistent_when_present():
    root = Path(__file__).resolve().parents[1] / "cleaned_full_v1_title_fixed"
    if not (root / "parent_documents.jsonl").exists():
        return
    parents = {row["document_id"]: row for row in map(json.loads, (root / "parent_documents.jsonl").open())}
    segments = list(map(json.loads, (root / "segment_records.jsonl").open()))
    chunks = list(map(json.loads, (root / "rag_chunks.jsonl").open()))
    assert all(row["review_status"] == "pending" for row in [*parents.values(), *segments, *chunks])
    assert all(parents[row["parent_document_id"]]["parent_document_type"] == row["parent_document_type"] for row in [*segments, *chunks])
    assert all(bool(row.get("section_title")) == bool(row.get("section_title_valid")) for row in [*segments, *chunks])
    assert all(not parent_title_issues(row["parent_title"], row["parent_document_type"]) for row in chunks)
