from __future__ import annotations

import json
from pathlib import Path

from pipeline_v2.classification_v242 import (
    is_concrete_accident_case,
    recover_title_v242,
    segment_type_v242,
)
from pipeline_v2.title_audit_v242 import audit_title_consistency_v242, title_checks


def test_guidance_marker_alone_is_not_accident_case():
    text = "关于加强化工企业泄漏管理的指导意见。泄漏会导致火灾、中毒和爆炸，应加强管理。"
    assert not is_concrete_accident_case(text, "关于加强化工企业泄漏管理的指导意见")
    assert segment_type_v242("关于加强化工企业泄漏管理的指导意见", text, "guidance_reference", 1, "valid", base_segment_type="full_document") == "full_document"


def test_three_case_evidence_classes_are_required():
    text = "2019年12月31日，某有限公司厂区发生中毒事故，造成3人死亡，调查认定直接原因是未检测。"
    assert is_concrete_accident_case(text, "某有限公司12·31中毒事故")
    assert segment_type_v242("某有限公司12·31中毒事故", text, "guidance_reference", 2, "valid", base_segment_type="guidance_section") == "accident_case_section"


def test_toc_with_dots_and_pages_is_not_list():
    title = "8 有限空间事故专项应急预案 ........................................................................198"
    text = title + "\n8.1 适用范围 ........................................198\n8.2 应急组织机构及职责"
    assert segment_type_v242(title, text, "enterprise_plan", 2, "toc_only", base_segment_type="list_fragment") == "toc_fragment"


def test_complete_onsite_plan_is_not_reference_sentence():
    title = "1.4.2 现场处置要点"
    text = "报警后立即疏散人员，设置警戒，组织救援；现场灭火并做好个人防护。"
    assert segment_type_v242(title, text, "government_or_regional_plan", 2, "valid", base_segment_type="reference_sentence") == "onsite_disposal_plan"


def test_complete_guidance_section_is_not_list_fragment():
    title = "5.3 救援注意事项"
    text = "一旦发生有限空间作业事故，应分析环境危害并判断救援方式。具备条件时采取非进入式救援，不具备条件时请求专业力量。"
    assert segment_type_v242(title, text, "guidance_reference", 2, "valid", base_segment_type="list_fragment") == "guidance_section"


def test_single_duty_item_is_list_fragment():
    assert segment_type_v242("（1）负责组织和发布", "负责组织和发布", "enterprise_plan", 2, "uncertain", base_segment_type="embedded_special_section") == "list_fragment"


def test_notification_title_recovers_attachment_name():
    decision = recover_title_v242(
        "news-show-1413",
        "通知全文粘连",
        "达州市安全生产委员会办公室关于发布《达州市危险化学品生产安全事故应急救援预案》的通知",
        "达州市安全生产委员会办公室关于发布《达州市危险化学品生产安全事故应急救援预案》的通知",
        "/tmp/news.html",
        "full_document",
        "government_or_regional_plan",
    )
    assert decision["title"] == "达州市危险化学品生产安全事故应急救援预案"
    assert decision["status"] == "valid"


def test_regulation_publication_note_recovers_formal_name():
    decision = recover_title_v242(
        "中华人民共和国应急管理部令（第13号）工贸企业有限空间作业安全规定",
        "发布说明",
        "中华人民共和国应急管理部令（第13号）\n《工贸企业有限空间作业安全规定》已经审议通过，现予公布。\n工贸企业有限空间作业安全规定\n第一条 正文",
        "",
        "/tmp/reg.html",
        "full_document",
        "regulation",
    )
    assert decision["title"] == "工贸企业有限空间作业安全规定"
    assert "审议通过" not in decision["title"]


def test_sds_first_aid_sentence_does_not_become_title():
    decision = recover_title_v242(
        "需要立即就医. 向现场的医生出示此安全技术说明书.",
        "",
        "需要立即就医。向现场的医生出示此安全技术说明书。",
        "",
        "/tmp/sds.pdf",
        "full_document",
        "sds",
    )
    assert decision["title"] == "化学品安全技术说明书"
    assert decision["status"] == "uncertain"


def test_sds_identity_is_traceable():
    decision = recover_title_v242(
        "需要立即就医. 向现场的医生出示此安全技术说明书.",
        "",
        "急救措施",
        "化学品安全技术说明书\n产品说明: 硝酸\nCAS号 7697-37-2\n一 化学品及企业标识",
        "/tmp/sds.pdf",
        "full_document",
        "sds",
    )
    assert decision["title"] == "硝酸 化学品安全技术说明书"
    assert decision["status"] == "valid"


def test_valid_title_hard_checks_are_zero_for_clean_heading():
    checks = title_checks("5.3 救援注意事项", "正文内容", "valid", "source_record_heading", "guidance_section")
    assert not any(checks[key] for key in ("over_80_chars", "contains_toc_dots", "contains_body_sentence", "notification_fulltext", "publication_note", "first_aid_sentence"))


def test_full_document_does_not_get_boundary_error():
    audit = audit_title_consistency_v242(
        document_id="synthetic",
        title="工贸企业有限空间作业安全规定",
        text="工贸企业有限空间作业安全规定\n第一条 正文。",
        segment_type="full_document",
        title_status="valid",
        title_source="document_title",
        sections=[],
    )
    assert audit["status"] in {"consistent", "not_applicable"}
    assert "boundary_error" not in audit["reasons"]


def test_invalid_residual_title_is_inconsistent():
    audit = audit_title_consistency_v242(
        document_id="synthetic",
        title="速按照本综合应急预案",
        text="速按照本综合应急预案执行。",
        segment_type="reference_sentence",
        title_status="invalid",
        title_source="source_record",
        sections=[],
    )
    assert audit["status"] == "inconsistent"


def test_v242_results_keep_fixed_sample_and_pending():
    path = Path("cleaned_v2_4_2/pilot_100_results_v2_4_2.json")
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 101
    assert len({row["parent_document_id"] for row in rows}) == 49
    assert all(row.get("review_status") == "pending" for row in rows)


def test_v242_known_segment_types_are_enabled():
    assert segment_type_v242("安全数据表", "正文", "enterprise_plan", 1, "valid", base_segment_type="full_document") == "appendix_sds"
    assert segment_type_v242("目录", "8 有限空间事故专项应急预案 ........198", "enterprise_plan", 1, "toc_only", base_segment_type="full_document") == "toc_fragment"


def test_no_valid_title_contains_forbidden_sentence():
    for title in ("需要立即就医。", "现印发给你们，请认真贯彻执行。", "48小时以上"):
        checks = title_checks(title, "", "valid", "source_record", "full_document")
        assert checks["contains_body_sentence"] or checks["notification_fulltext"] or checks["over_80_chars"] or checks["residual_fragment"] or checks["publication_note"]


def test_accident_case_evidence_not_keyword_only():
    assert not is_concrete_accident_case("本指南用于防止中毒、窒息、泄漏和火灾事故。", "事故预防指南")
