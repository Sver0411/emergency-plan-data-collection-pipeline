"""Regressions for the final semantic and RAG-content repair."""
from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path

from pipeline_v2.models_repair_v1 import RepairedParent
from pipeline_v2.semantic_fix_v1 import raw_integrity_by_content, resolve_real_source_path
from pipeline_v2.semantic_rules_v1 import (
    classify_parent_semantic,
    classify_segment_semantic,
    clean_html_semantic,
    invalid_parent_title,
    has_navigation_or_script_pollution,
    recover_parent_title_semantic,
    segment_rag_enabled,
    strong_sds_evidence,
)


def parent(title: str, source: str = "/tmp/document.pdf", old_type: str = "guidance_reference") -> dict:
    return {
        "document_id": "file-12345678", "parent_document_id": "file-12345678",
        "parent_title": title, "canonical_title": title, "source_file": source,
        "parent_document_type": old_type, "parse_status": "success", "rag_enabled": True,
    }


def sds_body(name: str = "Chlorine") -> str:
    return "\n".join([
        "SAFETY DATA SHEET (SDS)", f"Product name: {name}", "CAS No.: 7782-50-5",
        "Hazards identification", "Composition / information on ingredients",
        "First aid measures", "Fire fighting measures", "Accidental release measures",
        "Handling and storage", "Exposure controls / personal protection",
        "Physical and chemical properties", "Stability and reactivity",
        "Toxicological information", "Ecological information", "Disposal considerations",
        "Transport information", "Regulatory information", "Other information",
        "Substantive safety information. " * 20,
    ])


def test_complete_sds_overrides_supplier_and_regulation_vocabulary():
    text = sds_body() + "\nSupplier: Example Industrial Co., Ltd. Complies with OSHA regulations and GB/T standards."
    result = classify_parent_semantic(parent("ALC-SDS-P014 Chlorine", "/tmp/ALC-SDS-P014_Chlorine.pdf"), text)
    assert result["parent_document_type"] == "sds"
    assert strong_sds_evidence(text)["matched"]


def test_compilation_guide_is_regulation_not_enterprise_plan():
    text = "生产经营单位生产安全事故应急预案编制导则\n适用范围\n风险分析\n组织机构与职责\n应急响应\n应急处置\n应急保障\n" + "规范性要求。" * 80
    result = classify_parent_semantic(parent("生产经营单位生产安全事故应急预案编制导则", "/tmp/GB_T 29639-2020生产经营单位生产安全事故应急预案编制导则.pdf", "enterprise_plan"), text)
    assert result["parent_document_type"] == "regulation"


def test_concrete_enterprise_plan_not_organization_plan():
    text = "山东华鲁恒升化工股份有限公司编制，本预案适用于本公司内部。事故风险分析。组织机构与职责。应急响应。应急处置。应急保障。" * 12
    result = classify_parent_semantic(parent("山东华鲁恒升化工突发环境事件综合应急预案2025版", "/tmp/山东华鲁恒升化工突发环境事件综合应急预案2025版.pdf", "organization_plan"), text)
    assert result["parent_document_type"] == "enterprise_plan"


def test_enterprise_onsite_plan_not_accident_case():
    text = "山东石大胜华化工集团有限公司编制，本方案适用于本公司内部。事故风险分析。组织机构与职责。应急响应。现场处置。" * 15
    result = classify_parent_semantic(parent("山东石大胜华化工集团中毒窒息现场处置方案", "/tmp/山东石大胜华化工集团中毒窒息现场处置方案.pdf", "accident_case"), text)
    assert result["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"}


def test_actual_accident_report_requires_happened_accident_evidence():
    text = "2024年5月25日，临沂某有限公司发生火灾事故。事故经过。造成2人死亡。事故原因分析。责任追究。整改措施。" * 8
    result = classify_parent_semantic(parent("临沂某公司5·25火灾事故调查报告", "/tmp/临沂某公司5·25火灾事故调查报告.html"), text)
    assert result["parent_document_type"] == "accident_case"


def test_pubchem_has_one_non_rag_type():
    result = classify_parent_semantic(parent("PubChem-氯气", "/tmp/PubChem-氯气.json", "regulation"), '{"SourceID": "PubChem", "License": "CC0"}' * 20)
    assert result["parent_document_type"] == "chemical_reference"
    assert result["content_role"] == "chemical_reference"
    assert result["rag_enabled"] is False


def test_html_cleaner_removes_script_navigation_and_footer():
    raw = """<html><head><title>危险化学品预案</title><script>var x = new WebFXTreeItem();</script></head>
    <body><nav>首页 栏目导航 登录 注册</nav><main><h1>危险化学品事故应急预案</h1><p>本预案正文包含风险分析、应急响应和处置措施。</p><p>正文内容。</p></main><footer>版权所有 返回顶部</footer></body></html>""".encode()
    result = clean_html_semantic(raw)
    assert "WebFXTreeItem" not in result["normalized_text"]
    assert "版权所有" not in result["normalized_text"]
    assert result["javascript_hits"] == 0


def test_html_shell_fails_instead_of_becoming_full_document():
    result = clean_html_semantic(b"<html><body><script>var menu=1;</script><nav>home login</nav></body></html>")
    assert result["parse_status"] == "html_content_extraction_failed"


def test_ordinary_legal_use_of_registration_is_not_navigation_pollution():
    text = "企业应依法完成特种设备注册登记，并由安全管理部门保存登记资料。"
    assert has_navigation_or_script_pollution(text) is False
    assert has_navigation_or_script_pollution("首页 栏目导航 用户登录 返回顶部") is True


def test_front_matter_personnel_and_contacts_have_dedicated_types():
    front = {"content": "预案编号：ABC-01\n版本号：2026\n编制单位：某公司\n批准人：某某\n发布日期：2026年8月24日", "segment_type": "guidance_section"}
    personnel = {"content": "总指挥：张某\n副总指挥：李某\n组长：王某\n成员：赵某", "segment_type": "guidance_section"}
    contacts = {"content": "联系人：甲 13812345678\n联系人：乙 13912345678\n联系人：丙 13712345678", "segment_type": "guidance_section"}
    assert classify_segment_semantic(front, "enterprise_plan")[0] == "front_matter"
    assert classify_segment_semantic(personnel, "enterprise_plan")[0] == "personnel_table"
    assert classify_segment_semantic(contacts, "enterprise_plan")[0] == "contact_list"


def test_accident_case_section_only_under_accident_parent():
    row = {"content": "事故经过和原因分析只是指导文件中的示例。" * 20, "segment_type": "accident_case_section", "section_title": "示例"}
    assert classify_segment_semantic(row, "guidance_reference")[0] != "accident_case_section"
    assert classify_segment_semantic(row, "accident_case")[0] == "accident_case_section"


def test_context_segments_and_pubchem_cannot_enter_rag():
    segment = {"content": "人员名单" * 50, "segment_type": "personnel_table", "is_canonical": True}
    assert segment_rag_enabled(segment, {"rag_enabled": True, "parse_status": "success"}) is False
    segment["segment_type"] = "full_document"
    assert segment_rag_enabled(segment, {"rag_enabled": False, "parse_status": "success"}) is False


def test_parent_title_recovery_rejects_article_page_and_action_sentences():
    assert invalid_parent_title("第一条 为加强安全生产管理")
    assert invalid_parent_title("1 / 9")
    assert invalid_parent_title("建议由应急管理局对责任人员进行行政处罚。")
    result = recover_parent_title_semantic({**parent("第二条 适用本规定"), "parent_document_type": "regulation"}, "第二条 适用本规定\n正文", None)
    assert result["title"] != "第二条 适用本规定"


def test_sds_parent_title_uses_chemical_identity_not_page_number():
    text = "1 / 9\n化学品安全技术说明书（SDS）\n氢氧化钾\n产品中文名称 氢氧化钾\nCAS No. 1310-58-3\n" + sds_body("氢氧化钾")
    result = recover_parent_title_semantic({**parent("1 / 9 化学品安全技术说明书"), "parent_document_type": "sds"}, text)
    assert result["title"].startswith("氢氧化钾")
    assert "1 / 9" not in result["title"]


def test_semantic_models_still_forbid_approved():
    payload = {
        "document_id": "file-12345678", "parent_document_id": "file-12345678",
        "source_file": "/tmp/a.pdf", "relative_path": "a.pdf", "parent_document_type": "sds",
        "parent_title": "氯气化学品安全技术说明书", "parent_title_status": "valid",
        "parent_title_source": "sds_chemical_identity", "parse_status": "success", "review_status": "pending",
    }
    RepairedParent.model_validate(payload)
    payload["review_status"] = "approved"
    try:
        RepairedParent.model_validate(payload)
    except Exception:
        pass
    else:
        raise AssertionError("semantic-fixed records must remain pending")


def test_generated_semantic_output_is_cross_layer_consistent_when_present():
    root = Path(__file__).resolve().parents[1] / "cleaned_full_v1_semantic_fixed"
    if not (root / "parent_documents.jsonl").exists():
        return
    parents = {row["document_id"]: row for row in map(json.loads, (root / "parent_documents.jsonl").open())}
    segments = list(map(json.loads, (root / "segment_records.jsonl").open()))
    chunks = list(map(json.loads, (root / "rag_chunks.jsonl").open()))
    assert all(row["review_status"] == "pending" for row in [*parents.values(), *segments, *chunks])
    assert all(parents[row["parent_document_id"]]["parent_document_type"] == row["parent_document_type"] for row in [*segments, *chunks])
    assert not any("pubchem" in row["source_file"].lower() for row in chunks)


def test_manifest_long_name_is_resolved_by_sha_without_changing_raw(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    actual = raw / "truncated-name.html"
    actual.write_text("原始网页内容", encoding="utf-8")
    digest = hashlib.sha256(actual.read_bytes()).hexdigest()
    row = {"document_id": "file-12345678", "source_file": str(raw / ("超长文件名" * 50 + ".html")), "relative_path": "bad", "sha256": digest}
    resolved, alias = resolve_real_source_path(row, {digest: [actual.resolve()]}, raw)
    assert resolved["source_file"] == str(actual.resolve())
    assert alias and alias["sha256"] == digest
    ok, detail = raw_integrity_by_content([row], Counter({digest: 1}))
    assert ok and detail["missing_hash_counts"] == {}
