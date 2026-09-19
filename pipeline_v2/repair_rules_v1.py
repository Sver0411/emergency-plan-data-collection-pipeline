"""Deterministic repair rules for the first full cleaning result.

The functions in this module deliberately classify from document structure and
local segment text.  Stable identifiers are never inspected, so regression
fixtures cannot leak into production decisions.
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

from .normalization import normalize_text


SYSTEM_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
PARENT_TYPES = {
    "government_or_regional_plan", "enterprise_plan", "enterprise_group_plan",
    "organization_plan", "sds", "regulation", "guidance_reference",
    "accident_case", "chemical_catalog", "manual_review",
    "excluded_irrelevant", "reject",
}
SEGMENT_TYPES = {
    "full_document", "embedded_special_section", "onsite_disposal_plan",
    "guidance_section", "accident_case_section", "reference_sentence",
    "list_fragment", "toc_fragment", "table", "appendix_sds",
    "embedded_sds", "regulation_article", "sds_section",
}

SDS_MARKER_RE = re.compile(r"化学品安全技术说明书|安全技术说明书|安全数据表|安全资料表|安全資料表|物質安全資料表|\bM?SDS\b", re.I)
SDS_IDENTITY_RE = re.compile(r"化学品名称|产品名称|物质名称|化學品名稱|物品與廠商資料|化學品與廠商資料|化學文摘社登記號碼|CAS(?:号|號|\s*No\.?|\s*Number)?|UN(?:号|號|\s*No\.?)", re.I)
SDS_SECTION_PATTERNS = (
    r"危险性概述|危害辨识|危害辨識", r"成分.?组成信息|成分辨识|成分\/組成信息",
    r"急救措施", r"消防措施|灭火措施|滅火措施", r"泄漏应急处理|洩漏處理方法|泄漏处置",
    r"操作处置与储存|操作和储存|安全處置與儲存方法", r"接触控制.?个体防护|暴露控制.?個人防護|暴露預防措施",
    r"理化特性|物理及化學性質", r"稳定性和反应性|安定性及反應性", r"毒理学信息|毒性資料", r"生态学信息|生態資料",
    r"废弃处置|廢棄處置方法", r"运输信息|運送資料", r"法规信息|法規資料",
)
PLAN_CORE_PATTERNS = (
    r"编制目的", r"适用范围", r"事故风险分析|危险性分析|风险分析",
    r"组织机构(?:和|与)?职责|应急组织", r"应急响应|响应分级|响应程序",
    r"应急处置|处置措施|现场处置", r"应急保障|保障措施", r"应急结束|附则",
)
GOV_SUBJECT_RE = re.compile(r"(?:人民政府|管委会|安委会|应急管理(?:局|厅|部)|街道办事处|乡人民政府|镇人民政府)")
GOV_SCOPE_RE = re.compile(r"(?:本省|全省|本市|全市|本区|全区|本县|全县|本镇|全镇|本街道|辖区|成员单位职责|请求上级政府支援|区域资源调度)")
GOV_TITLE_RE = re.compile(r"(?:省|市|区|县|镇|乡|街道|开发区|园区).{0,45}(?:应急预案|救援预案|处置预案)")
ENTERPRISE_SUBJECT_RE = re.compile(r"(?:有限公司|股份公司|集团公司|有限责任公司|公司|厂|矿|站|生产经营单位)")
ENTERPRISE_SCOPE_RE = re.compile(r"(?:本公司|本企业|我公司|本厂|本矿|厂区|矿区|公司内部|企业内部|本单位)")
ORGANIZATION_RE = re.compile(r"(?:大学|学院|学校|医院|研究院|事业单位).{0,30}(?:应急预案|事故预案)")
ACCIDENT_CASE_TITLE_RE = re.compile(r"(?:事故调查报告|事故调查处理报告|事故通报|事故整改评估|事故案例|盲目施救导致伤亡)")
ACCIDENT_DATE_RE = re.compile(r"(?:20\d{2}年\d{1,2}月\d{1,2}日|[“\"']?\d{1,2}[·\.\-]\d{1,2}[”\"']?\s*(?:较大|重大|一般|特别重大)?事故)")
ACCIDENT_DETAIL_PATTERNS = (
    r"事故经过|事故发生经过", r"人员伤亡|造成\s*\d+\s*人|死亡\s*\d+\s*人|受伤\s*\d+\s*人",
    r"事故原因|原因分析", r"责任追究|事故责任", r"整改措施|防范措施|整改评估",
)
REG_TITLE_RE = re.compile(r"(?:法|条例|规定|办法|规程|标准|规范|导则|实施细则|管理规则)(?:\s|$|（|\()")
REG_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百零〇0-9]+条")
GUIDANCE_TITLE_RE = re.compile(r"指南|指导意见|指导手册|实施解读|标准解读|应急处置原则|安全措施和应急处置原则")
CHEM_CATALOG_RE = re.compile(r"危险化学品目录|危化品目录|化学品名录|危险化学品名录|目录指南")
IRRELEVANT_RE = re.compile(r"政府信息公开指南|信息公开目录|网站导航|登录|用户登录|404\s*(?:not found|错误|页面不存在)|新闻列表|访问出错", re.I)
TOC_LINE_RE = re.compile(r"^\s*(?:第?[一二三四五六七八九十0-9]+[章节部分、.．\s]*)?.{2,80}(?:\.{4,}|…{2,}|·{4,})\s*\d{1,4}\s*$")
REFERENCE_RE = re.compile(r"^(?:执行|参照|按照|启动|相关内容见).{0,80}(?:预案|规定|章节)(?:执行|处置|实施)?[。；;]?$", re.S)
LIST_ITEM_RE = re.compile(r"^\s*(?:\(?[一二三四五六七八九十0-9]+\)?[、.．]|[-—•])\s*[^\n]{2,180}$")
ONSITE_RE = re.compile(r"现场处置方案|现场处置要点|应急处置要点")
SPECIAL_RE = re.compile(r"专项应急预案|事故应急预案|应急处置方案")


def is_system_file(path_or_name: str | Path) -> bool:
    return Path(path_or_name).name.lower() in SYSTEM_FILE_NAMES


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8", errors="replace")).hexdigest()


def _score_patterns(text: str, patterns: tuple[str, ...]) -> int:
    return sum(bool(re.search(pattern, text, re.I)) for pattern in patterns)


def _first_lines(text: str, count: int = 30) -> str:
    return "\n".join(line.strip() for line in text.splitlines()[:count] if line.strip())


def looks_like_sds(text: str) -> tuple[bool, list[str], float]:
    head = _first_lines(text, 100)
    marker = bool(SDS_MARKER_RE.search(head))
    identity = bool(SDS_IDENTITY_RE.search(head[:12000]))
    sections = _score_patterns(text[:60000], SDS_SECTION_PATTERNS)
    cas_or_un = bool(re.search(r"\b\d{2,7}-\d{2}-\d\b|\bUN\s*\d{4}\b", text, re.I))
    evidence = []
    if marker:
        evidence.append("sds_explicit_marker")
    if identity:
        evidence.append("chemical_identity_section")
    if cas_or_un:
        evidence.append("cas_or_un_identifier")
    evidence.append(f"sds_standard_sections={sections}")
    # A partial but unmistakable SDS is retained for manual review.  A CAS
    # alone, or a supplier/company name, never reaches this threshold.
    matched = (marker and identity and sections >= 3) or (marker and sections >= 5)
    confidence = min(0.98, 0.58 + 0.045 * sections + 0.08 * identity + 0.08 * cas_or_un) if matched else 0.0
    return matched, evidence, round(confidence, 3)


def _accident_case_evidence(text: str, title: str) -> tuple[bool, list[str], float]:
    lead = f"{title}\n{text[:50000]}"
    title_marker = bool(ACCIDENT_CASE_TITLE_RE.search(title or _first_lines(text, 5)))
    has_date = bool(ACCIDENT_DATE_RE.search(lead))
    details = _score_patterns(lead, ACCIDENT_DETAIL_PATTERNS)
    has_place_or_company = bool(re.search(r"(?:有限公司|公司|项目|矿|厂|市|区|县).{0,30}(?:事故|发生)", lead))
    matched = (title_marker and details >= 2) or (has_date and has_place_or_company and details >= 2)
    evidence = [f"accident_title_marker={title_marker}", f"accident_date={has_date}", f"accident_detail_groups={details}", f"accident_place_or_company={has_place_or_company}"]
    return matched, evidence, round(min(0.97, 0.54 + 0.08 * details + 0.08 * has_date + 0.08 * title_marker), 3) if matched else 0.0


def classify_parent_repaired(
    *, text: str, title: str = "", source_file: str = "", parse_status: str = "success",
) -> dict[str, Any]:
    """Classify one physical file from dominant document structure."""
    normalized = normalize_text(text)
    source_label = Path(source_file).stem
    head = f"{title}\n{source_label}\n{_first_lines(normalized, 120)}"
    if is_system_file(source_file):
        return {"status": "ignored_system_file", "parent_document_type": None, "confidence": 1.0, "evidence": ["system_file_name"]}
    if parse_status != "success" or len(normalized) < 80:
        return {"status": "classified", "parent_document_type": "reject", "confidence": 0.96, "evidence": ["unreadable_or_insufficient_text"], "requires_manual_review": True}

    if IRRELEVANT_RE.search(head[:12000]) and not re.search(r"应急预案|安全技术说明书|事故调查报告", head[:12000]):
        return {"status": "classified", "parent_document_type": "excluded_irrelevant", "confidence": 0.94, "evidence": ["irrelevant_or_navigation_page"], "requires_manual_review": False}

    sds, sds_evidence, sds_conf = looks_like_sds(f"{title}\n{source_label}\n{normalized}")
    if sds:
        return {"status": "classified", "parent_document_type": "sds", "confidence": sds_conf, "evidence": sds_evidence, "requires_manual_review": sds_conf < 0.85}

    if CHEM_CATALOG_RE.search(head[:4000]) and (re.search(r"CAS|序号|品名|别名", normalized[:15000], re.I) or normalized.count("\t") > 8):
        return {"status": "classified", "parent_document_type": "chemical_catalog", "confidence": 0.93, "evidence": ["chemical_catalog_title_and_table_structure"], "requires_manual_review": False}

    accident, accident_evidence, accident_conf = _accident_case_evidence(normalized, f"{title}\n{source_label}")
    explicit_accident_title = bool(ACCIDENT_CASE_TITLE_RE.search(f"{title}\n{source_label}"))
    if accident and explicit_accident_title:
        return {"status": "classified", "parent_document_type": "accident_case", "confidence": accident_conf, "evidence": accident_evidence, "requires_manual_review": accident_conf < 0.82}

    # Administrative issuer/scope dominates incidental mentions of schools,
    # hospitals and enterprises in member-unit responsibility sections.
    gov_subject = bool(GOV_SUBJECT_RE.search(head[:10000]))
    gov_scope = bool(GOV_SCOPE_RE.search(normalized[:25000]))
    gov_title = bool(GOV_TITLE_RE.search(head[:5000]))
    if gov_title and (gov_subject or gov_scope):
        return {"status": "classified", "parent_document_type": "government_or_regional_plan", "confidence": 0.94 if gov_subject and gov_scope else 0.87, "evidence": [f"administrative_title={gov_title}", f"administrative_issuer={gov_subject}", f"jurisdiction_scope={gov_scope}"], "requires_manual_review": False}

    # Guidance title outranks quoted article structures in a guide.
    if GUIDANCE_TITLE_RE.search(head[:5000]):
        relevant = bool(re.search(r"应急|安全生产|危险化学品|危化品|有限空间|事故|泄漏|中毒|窒息", normalized[:25000]))
        kind = "guidance_reference" if relevant else "excluded_irrelevant"
        return {"status": "classified", "parent_document_type": kind, "confidence": 0.93 if relevant else 0.88, "evidence": ["guidance_title", f"project_relevant={relevant}"], "requires_manual_review": False}

    # Regulations require the source itself to have a formal title/continuous
    # article or standard structure, rather than merely quoting laws.
    plan_title = bool(re.search(r"(?:专项|综合|生产安全事故|突发事件|现场处置).{0,16}应急预案|应急预案", head[:4000]))
    regulation_title = bool(REG_TITLE_RE.search(f"{title}\n{source_label}\n{_first_lines(normalized, 5)}"))
    formal_regulation_title = bool(re.search(r"(?:法|条例|管理办法|规定|规程|标准|规范|导则|实施细则|管理规则)$", title.strip().strip("《》")))
    article_count = len(REG_ARTICLE_RE.findall(normalized[:50000]))
    standard_marker = bool(re.search(r"(?:GB(?:/T)?|AQ|DB\d*|HJ|国家标准|行业标准)\s*[/T\-]?\s*\d", head[:5000], re.I))
    if (formal_regulation_title and article_count >= 3) or (not plan_title and ((regulation_title and article_count >= 3) or standard_marker)):
        return {"status": "classified", "parent_document_type": "regulation", "confidence": 0.95 if article_count >= 5 else 0.88, "evidence": [f"formal_regulation_title={regulation_title}", f"continuous_articles={article_count}", f"standard_marker={standard_marker}"], "requires_manual_review": False}

    if ORGANIZATION_RE.search(head[:6000]):
        return {"status": "classified", "parent_document_type": "organization_plan", "confidence": 0.9, "evidence": ["organization_internal_plan_title"], "requires_manual_review": False}

    enterprise_subject = bool(ENTERPRISE_SUBJECT_RE.search(head[:5000]))
    enterprise_scope = bool(ENTERPRISE_SCOPE_RE.search(normalized[:18000]))
    plan_cores = _score_patterns(normalized[:70000], PLAN_CORE_PATTERNS)
    if plan_title and enterprise_subject and enterprise_scope and plan_cores >= 4:
        group = bool(re.search(r"集团(?:有限公司|公司)|所属企业|下属单位", head[:6000]))
        ptype = "enterprise_group_plan" if group else "enterprise_plan"
        return {"status": "classified", "parent_document_type": ptype, "confidence": min(0.96, 0.72 + 0.04 * plan_cores), "evidence": ["enterprise_plan_title", "concrete_enterprise_subject", "enterprise_internal_scope", f"plan_core_groups={plan_cores}"], "requires_manual_review": False}

    if accident and not plan_title:
        return {"status": "classified", "parent_document_type": "accident_case", "confidence": accident_conf, "evidence": accident_evidence, "requires_manual_review": accident_conf < 0.82}

    # A government title without enough issuer context stays reviewable rather
    # than being promoted into enterprise data.
    if gov_title or gov_subject:
        return {"status": "classified", "parent_document_type": "government_or_regional_plan", "confidence": 0.76, "evidence": ["administrative_plan_partial_evidence"], "requires_manual_review": True}

    if plan_title and plan_cores >= 3:
        return {"status": "classified", "parent_document_type": "manual_review", "confidence": 0.56, "evidence": ["plan_structure_but_issuer_scope_uncertain", f"plan_core_groups={plan_cores}"], "requires_manual_review": True}

    relevant = bool(re.search(r"应急|安全生产|危险化学品|有限空间|中毒|窒息|泄漏|火灾|爆炸", normalized[:30000]))
    if relevant and len(normalized) >= 300:
        return {"status": "classified", "parent_document_type": "guidance_reference", "confidence": 0.62, "evidence": ["project_relevant_reference_content"], "requires_manual_review": True}
    return {"status": "classified", "parent_document_type": "excluded_irrelevant", "confidence": 0.72, "evidence": ["no_project_relevant_document_structure"], "requires_manual_review": False}


def accident_category_repaired(title: str, text: str, parent_type: str) -> tuple[str, str]:
    combined = f"{title}\n{text[:16000]}"
    if parent_type in {"sds", "regulation", "chemical_catalog", "excluded_irrelevant", "reject"}:
        return "not_applicable", ""
    if re.search(r"高空作业|高处作业|高处坠落", combined):
        return "other", "fall_from_height"
    candidates = [
        ("confined_space", r"有限空间|受限空间"),
        ("poisoning_asphyxia", r"中毒.{0,4}窒息|硫化氢中毒|缺氧窒息"),
        ("chemical_leakage", r"危险化学品泄漏|危化品泄漏|液氨泄漏|氯气泄漏|泄漏事故"),
        ("fire_explosion", r"火灾.{0,4}爆炸|爆炸事故|火灾事故"),
        ("transport_accident", r"运输事故|交通事故|车辆伤害"),
        ("natural_disaster", r"自然灾害|防洪防汛|地震|台风|水灾"),
        ("collapse", r"坍塌|倒塌"),
    ]
    title_hits = [kind for kind, pattern in candidates if re.search(pattern, title)]
    if len(title_hits) == 1:
        return title_hits[0], ""
    hits = [kind for kind, pattern in candidates if re.search(pattern, combined)]
    unique = list(dict.fromkeys(hits))
    if len(unique) >= 2:
        return "multi_hazard", ""
    if unique:
        return unique[0], ""
    return "other", ""


def _directory_ratio(text: str) -> tuple[float, int, int]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    toc = sum(bool(TOC_LINE_RE.match(line)) for line in lines)
    return toc / max(1, len(lines)), toc, len(lines)


def classify_segment_repaired(
    *, title: str, text: str, parent_type: str, is_parent_full: bool = False,
) -> tuple[str, dict[str, Any]]:
    local = normalize_text(text)
    ratio, toc_lines, lines = _directory_ratio(local)
    sentence_count = len(re.findall(r"[。！？；]", local))
    core_count = _score_patterns(local, PLAN_CORE_PATTERNS)
    action_count = _score_patterns(local, (r"疏散", r"警戒", r"扑救|灭火", r"堵漏|切断泄漏源", r"救援", r"应采取以下措施|立即"))
    audit = {"segment_classification_text_source": "raw_text", "segment_local_text_length": len(local), "parent_context_used": False, "toc_line_ratio": round(ratio, 4), "toc_lines": toc_lines, "line_count": lines, "plan_core_groups": core_count, "action_groups": action_count}
    if is_parent_full and len(local) >= 120 and (core_count >= 3 or parent_type in {"sds", "regulation", "accident_case", "chemical_catalog", "guidance_reference"}):
        return "full_document", audit
    if ratio >= 0.55 and toc_lines >= 2 and sentence_count < 3 and action_count < 2:
        return "toc_fragment", audit
    if ONSITE_RE.search(title) and len(local) >= 120 and action_count >= 2:
        return "onsite_disposal_plan", audit
    if REFERENCE_RE.fullmatch(local.strip()) and len(local) <= 180:
        return "reference_sentence", audit
    if (LIST_ITEM_RE.fullmatch(local.strip()) or len(local) <= 180) and sentence_count <= 2 and core_count < 2:
        return "list_fragment", audit
    if parent_type == "sds" and _score_patterns(local, SDS_SECTION_PATTERNS) >= 1:
        return "sds_section", audit
    if parent_type == "regulation" and (REG_ARTICLE_RE.search(local) or re.search(r"第[一二三四五六七八九十0-9]+[章节]", title)):
        return "regulation_article", audit
    if parent_type == "accident_case" and len(local) >= 120:
        return "accident_case_section", audit
    if parent_type == "guidance_reference" and len(local) >= 120:
        return "guidance_section", audit
    if SPECIAL_RE.search(title) and len(local) >= 300 and core_count >= 2:
        return "embedded_special_section", audit
    if ONSITE_RE.search(title) and len(local) >= 120:
        return "onsite_disposal_plan", audit
    if "\t" in local and lines >= 3:
        return "table", audit
    return "guidance_section" if len(local) >= 300 else "list_fragment", audit


def recover_parent_title(text: str, existing_title: str, source_file: str, parent_type: str) -> tuple[str, str, str]:
    def valid(value: str) -> bool:
        value = value.strip().strip("《》")
        return bool(value and len(value) <= 120 and not TOC_LINE_RE.match(value) and not re.search(r"现予公布|现印发|请结合实际|请认真贯彻|需要立即|负责组织|第一节\s*一般规定|^\d+\s*应急响应$", value))

    if parent_type == "sds":
        identity_patterns = (
            r"(?:化学品名称|产品名称|物質名稱|化學品名稱)\s*[:：]\s*([^\n；;]{1,60})",
            r"^\s*([^\n]{1,50})\s*(?:化学品安全技术说明书|安全数据表|安全资料表|物質安全資料表)",
        )
        for pattern in identity_patterns:
            match = re.search(pattern, text[:16000], re.I | re.M)
            if match:
                chemical = re.sub(r"\s+", " ", match.group(1)).strip(" ：:;；")
                if valid(chemical) and not re.search(r"供应商|厂商|公司", chemical):
                    return f"{chemical} 化学品安全技术说明书", "valid", "sds_chemical_identity"
        filename = re.sub(r"[-_][0-9a-f]{10,}$", "", Path(source_file).stem, flags=re.I)
        if valid(filename) and re.search(r"SDS|MSDS|安全数据|安全技术说明书", filename, re.I):
            return filename, "uncertain", "cleaned_filename"
        return "化学品安全技术说明书", "uncertain", "generic_sds_title"

    filename = re.sub(r"-[0-9a-f]{10,}$", "", Path(source_file).stem, flags=re.I)
    filename = re.sub(r"^(?:PDF|DOCX?|HTML)\s+", "", filename, flags=re.I).strip(" _-")
    filename_has_formal_title = bool(re.search(r"应急预案|事故调查报告|事故通报|规程|规定|办法|条例|标准|规范|导则|指南|指导意见", filename))
    existing_looks_like_inner_heading = bool(re.match(r"^\s*\d+(?:[.．]\d+)*\s+", existing_title))
    existing_conflicts_with_plan_file = bool(re.search(r"应急预案", filename) and re.search(r"管理办法|一般规定|应急响应$", existing_title))
    if filename_has_formal_title and valid(filename) and (not valid(existing_title) or existing_looks_like_inner_heading or existing_conflicts_with_plan_file):
        return filename, "uncertain", "cleaned_filename_conflict_recovery"

    embedded_book = re.search(r"《([^》]{2,100}(?:应急预案|调查报告|规程|规定|办法|条例|指南|指导意见))》", existing_title)
    if embedded_book and valid(embedded_book.group(1)):
        return embedded_book.group(1), "valid", "embedded_formal_title"
    prefix_plan = re.search(r"^《?([^\n]{2,100}?应急预案)(?:》|已经|现印发|$)", existing_title)
    if prefix_plan and valid(prefix_plan.group(1)):
        return prefix_plan.group(1), "valid", "trimmed_notification_title"
    if valid(existing_title):
        return existing_title.strip().strip("《》"), "valid", "existing_parent_title"
    for line in [item.strip().strip("《》") for item in text.splitlines()[:80] if item.strip()]:
        if valid(line) and re.search(r"应急预案|事故调查报告|事故通报|规程|规定|办法|条例|标准|指南|指导意见|目录", line):
            return line, "valid", "raw_text_document_title"
    if valid(filename):
        return filename, "uncertain", "cleaned_filename"
    return "", "missing", "missing"


def data_quality_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    warnings = set(record.get("warnings") or [])
    if record.get("parse_status") != "success": reasons.append("parse_failed")
    if record.get("parent_title_status") in {"missing", "invalid"}: reasons.append("parent_title_missing_or_invalid")
    if record.get("classification_conflict"): reasons.append("parent_type_conflict")
    if record.get("title_consistency") == "inconsistent": reasons.append("title_body_inconsistent")
    if warnings & {"sds_identity_conflict", "cas_conflict", "garbled_text", "document_truncated", "directory_as_body", "body_as_directory", "extension_content_conflict"}:
        reasons.extend(sorted(warnings & {"sds_identity_conflict", "cas_conflict", "garbled_text", "document_truncated", "directory_as_body", "body_as_directory", "extension_content_conflict"}))
    return list(dict.fromkeys(reasons))


def professional_review_reasons(text: str) -> list[str]:
    groups = {
        "first_aid_or_medical": r"急救|人工呼吸|心肺复苏|医疗处置|催吐|洗胃",
        "firefighting": r"消防|灭火|扑救",
        "leak_control": r"堵漏|泄漏处置|切断泄漏源|中和|洗消",
        "personal_protection": r"个人防护|防护服|呼吸器|防毒面具",
        "emergency_response": r"应急响应|疏散|警戒|救援",
        "technical_parameters": r"\d+(?:\.\d+)?\s*(?:mg/m3|mg\/m³|ppm|MPa|kPa|℃)",
    }
    return [name for name, pattern in groups.items() if re.search(pattern, text, re.I)]


def parse_html_local_repaired(raw: bytes) -> dict[str, Any]:
    match = from_bytes(raw).best()
    encoding = match.encoding if match else "utf_8"
    decoded = str(match) if match else raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "html.parser")
    title = html.unescape(soup.title.get_text(" ", strip=True)) if soup.title else ""
    h1 = html.unescape(soup.h1.get_text(" ", strip=True)) if soup.h1 else ""
    for node in soup.select("script,style,noscript,svg,nav,footer,header,aside,form,menu,.nav,.navbar,.footer,.header,.sidebar,.menu,.breadcrumb"):
        node.decompose()
    body = soup.body or soup
    text = normalize_text(body.get_text("\n", strip=True))
    label = "content"
    sample = f"{title}\n{h1}\n{text[:5000]}"
    if re.search(r"404\s*(?:not found|错误|页面不存在)|页面不存在", sample, re.I): label = "404"
    elif re.search(r"用户登录|账号登录|请输入密码|登录系统", sample): label = "login_page"
    elif len(text) < 120: label = "empty_shell"
    return {"parser": "html_local_repaired", "encoding": encoding, "title": title, "h1": h1, "raw_text": text, "normalized_text": text, "text_length": len(text), "html_page_type": label, "parse_status": "success" if len(text) >= 120 else "failed"}


def segment_is_rag_eligible(segment: dict[str, Any], parent: dict[str, Any]) -> bool:
    if segment.get("review_status") != "pending": return False
    if not segment.get("is_canonical", True): return False
    if parent.get("parse_status") != "success": return False
    if parent.get("parent_document_type") in {"excluded_irrelevant", "reject"}: return False
    if not parent.get("parent_title"): return False
    if segment.get("segment_type") in {"toc_fragment", "list_fragment", "reference_sentence"}: return False
    return len(str(segment.get("content") or "").strip()) >= 50
