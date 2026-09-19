"""Evidence-only rules for the final title and exclusion repair.

The functions in this module deliberately do not inspect document IDs.  They
operate on the parsed local text, file metadata and the already frozen parent
type.  Missing evidence produces an uncertain/manual-review result instead of
an invented title.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .normalization import normalize_text
from .semantic_rules_v1 import (
    ACCIDENT_TITLE_RE,
    ENTERPRISE_NAME_RE,
    GOV_RE,
    GOV_TITLE_RE,
    GUIDANCE_RE,
    IRRELEVANT_RE,
    SDS_MARKER_RE,
    accident_evidence,
    clean_filename_title,
    classify_parent_semantic,
    plan_structure_evidence,
    strong_sds_evidence,
)


SDS_VALUE_LABELS = (
    "化学品中文(英文)名称, 化学品俗名或商品名", "化学品中文 (英文 )名称 , 化学品俗名或商品名",
    "化学品中文名称", "产品中文名称", "产品名称", "化学品名称", "化學品名稱", "注册名称", "註冊名稱", "商品名称", "物质名称",
    "Chinese product name", "Product name", "Substance name", "Product identifier",
)
SDS_STOP_LABELS = (
    *SDS_VALUE_LABELS,
    "化学品英文名称", "产品英文名称", "英文名称", "英文名", "化学品俗名", "商品名",
    "企业名称", "生产企业", "制造商", "供应商", "推荐用途", "限制用途", "产品编号",
    "SDS编号", "SDS No", "CAS No", "CAS号", "UN No", "UN号", "EC No", "分子式",
    "版本号", "报告编号", "编制日期", "修订日期", "紧急联系电话", "地址", "邮编",
)
SDS_JUNK_RE = re.compile(
    r"(?:依据|根据)\s*GB/T|化学品及企业标识|化学品英文名称|产品英文名称|第一部分|"
    r"中国现有化学品名录|IECSC|供应商|企业名称|推荐用途|SDS\s*(?:No|编号)|"
    r"^\s*\d+\s*/\s*\d+\s*$",
    re.I,
)
SDS_GENERIC_RE = re.compile(r"^(?:化学品|产品|物质|安全资料表|安全数据表|化学品安全技术说明书|Safety Data Sheet|file|document|download|附件|资料)$", re.I)
SDS_SECTION_NAME_RE = re.compile(
    r"^(?:化学品及企业标识|化學品與廠商資料|物品與廠商資料|危险性概述|危害辨识|危害辨識|成分.?组成信息|組成.?成分|急救措施|消防措施|泄漏应急处理|"
    r"操作处置与储存|接触控制.?个体防护|理化特性|稳定性和反应性|毒理学信息|生态学信息|"
    r"废弃处置|运输信息|法规信息|其他信息|Hazards? identification|Composition|First.?aid measures|"
    r"Fire.?fighting measures|Accidental release measures|Handling and storage|Exposure controls?|"
    r"Physical and chemical properties|Stability and reactivity|Toxicological information|"
    r"Ecological information|Disposal considerations|Transport information|Regulatory information|Other information)$",
    re.I,
)
SDS_STOP_RE = re.compile("|".join(sorted((re.escape(item) for item in SDS_STOP_LABELS), key=len, reverse=True)), re.I)

PLAN_TITLE_RE = re.compile(
    r"(?:突发环境事件|生产安全事故|安全生产事故|危险化学品(?:泄漏)?事故|危险化学品泄漏事故|"
    r"中毒(?:和|与)?窒息事故|有限空间事故|火灾(?:爆炸)?事故|瓦斯煤尘事故|"
    r"天然气保供突发事件|液氨事故|环境突发事件|综合|专项)?"
    r"(?:综合|专项)?(?:应急预案|现场处置方案|应急处置方案)",
)
SPECIFIC_ENTERPRISE_RE = re.compile(
    r"[\u3400-\u9fffA-Za-z0-9（）()·]{2,70}(?:有限责任公司|股份有限公司|有限公司|集团公司|集团有限责任公司|"
    r"煤矿|矿业有限公司|集团)"
)
FORMAL_RELATED_RE = re.compile(
    r"危险化学品|危化品|有限空间|中毒|窒息|泄漏|安全生产|应急预案|现场处置|"
    r"事故调查|事故案例|事故评估|消防|化工|SDS|MSDS|安全技术说明书",
    re.I,
)
ACCIDENT_COLLECTION_RE = re.compile(
    r"事故典型案例|典型事故(?:案例|警示)|事故案例(?:汇编|集)?|事故调查报告|事故评估报告|"
    r"盲目施救导致伤亡|事故防范和整改措施落实情况评估",
)
FORMAL_REGULATION_RE = re.compile(r"(?:规定|办法|条例|规程|准则|标准|导则|细则)(?:（[^）]*）)?$")
GUIDANCE_TITLE_RE = re.compile(r"指南|指导意见|指导手册|实施解读|标准解读|应急处置原则|安全措施|工作方案|专项整治")

PURE_NUMBER_RE = re.compile(r"^\d{6,}$")
HASH_RE = re.compile(r"^(?:[0-9a-f]{16,}|[A-Za-z0-9_-]{20,})$", re.I)
DATE_ONLY_RE = re.compile(r"^(?:19|20)\d{2}(?:\s*[年./-]\s*\d{1,2}(?:\s*[月./-]\s*\d{1,2}\s*日?)?)?$")
FILEISH_RE = re.compile(r"^(?:aqyjyba|\d{8,}[_-]?\d*|[A-Za-z0-9_-]+\.(?:pdf|docx?|html?))$", re.I)
BODY_START_RE = re.compile(r"^(?:负责|组织|采取|立即|按照|根据|结合|本预案为|事故发生后|发生事故后|需要|应当|由|对|为确保|为加强|经|现予|请认真)")
PERSON_ROLE_RE = re.compile(r"(?:总指挥|副总指挥|组长|副组长|联系人|党支部书记|工程技术管理人员|姓名|职务)\s*[:：]?")
FIELD_STICK_RE = re.compile(
    r"化学品英文名称|产品英文名称|化学品及企业标识|供应商详细信息|化学品与厂商资料|"
    r"依据\s*GB/T|推荐用途|企业名称|\d+\s*/\s*\d+",
    re.I,
)
GENERIC_ENTERPRISE_TITLE_RE = re.compile(r"^(?:突发环境事件|生产安全事故|危险化学品事故)?(?:综合|专项)?应急预案$")

SECTION_ACTION_RE = re.compile(
    r"^(?:负责|组织|采取|立即|按照|根据|事故发生|发生事故|开展|编制|发布|修订|确认|需要|应当|"
    r"首先|其次|对|由|为|给患者|作业现场负责人)"
)
SECTION_DATE_SENTENCE_RE = re.compile(r"^(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:，|,|\s+).+")
SECTION_DATE_ONLY_RE = re.compile(r"^(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?(?:修订|发布|实施)?$")
SECTION_NUMERIC_RE = re.compile(r"^[\d\s./%-]{2,}$")
SECTION_HEADING_RE = re.compile(
    r"(?:总则|编制目的|适用范围|事故风险|危险性|组织机构|职责|预防|预警|信息报告|应急响应|"
    r"响应分级|处置措施|现场处置|应急处置|救援注意事项|应急保障|应急结束|后期处置|附则|"
    r"化学品及企业标识|危险性概述|成分.?组成|急救措施|消防措施|泄漏应急处理|操作处置与储存|"
    r"接触控制|理化特性|稳定性和反应性|毒理学|生态学|废弃处置|运输信息|法规信息|其他信息|"
    r"事故经过|原因分析|责任认定|整改措施|防范措施|Personal precautions|First.?aid|Fire.?fighting|"
    r"Accidental release|Handling and storage|Exposure controls|Toxicological|Ecological)",
    re.I,
)


def _clean_candidate(value: str) -> str:
    value = html.unescape(value).replace("\u3000", " ")
    value = re.sub(r"^[\s:：|丨/]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" \t:：,，;；|丨")
    value = re.sub(r"\s*[*＊]?依据\s*GB/T.*$", "", value, flags=re.I)
    value = re.sub(r"\s*(?:化学品安全技术说明书|安全资料表（?SDS）?|Safety Data Sheet)\s*$", "", value, flags=re.I)
    return value.strip(" -_")


def valid_chemical_name(value: str) -> bool:
    if not value or len(value) > 80 or SDS_GENERIC_RE.fullmatch(value) or SDS_SECTION_NAME_RE.fullmatch(value):
        return False
    if SDS_JUNK_RE.search(value) or re.search(r"[。！？!?]|(?:有限公司|供应商|地址|电话|教学|模板|指南|填写说明)", value):
        return False
    if not re.search(r"[\u3400-\u9fffA-Za-z]", value):
        return False
    return True


def _value_after_label(text: str, label: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(re.escape(label), text[:50000], re.I):
        tail = text[match.end():match.end() + 500]
        tail = re.sub(r"^[\s\t|丨]*[:：]?[\s\t|丨]*", "", tail)
        stop = SDS_STOP_RE.search(tail)
        if stop:
            tail = tail[:stop.start()]
        # A field value cannot consume a page, paragraph or table row.
        pieces = [piece.strip() for piece in re.split(r"[\r\n|丨\t]+", tail) if piece.strip()]
        if not pieces:
            continue
        candidate = _clean_candidate(pieces[0])
        # Chinese SDS fields sometimes list a one-character abbreviation,
        # followed by the explicit commonly used name.
        aliases = [item.strip() for item in re.split(r"[;；]", candidate) if item.strip()]
        if len(aliases) > 1 and len(aliases[0]) <= 1:
            candidate = aliases[1]
        elif aliases:
            candidate = aliases[0]
        if valid_chemical_name(candidate):
            values.append(candidate)
    return values


def extract_sds_chemical_precise(text: str, source_file: str = "") -> dict[str, Any]:
    """Extract one SDS identity value and stop before adjacent field labels."""
    normalized = normalize_text(text)
    normalized = re.sub(
        r"化学品中文\s*\(\s*英文\s*\)\s*名称\s*[,，]\s*化学品俗名或\s*商品名",
        "化学品中文名称",
        normalized,
    )
    for label in SDS_VALUE_LABELS:
        values = _value_after_label(normalized, label)
        if values:
            return {"chemical_name": values[0], "source": f"identity_field:{label}", "confidence": 0.98}

    # Some supplier PDFs put the identity on its own line immediately after
    # the document title.  Only accept a short, standalone, non-field line.
    lines = [line.strip() for line in normalized[:12000].splitlines() if line.strip()]
    for index, line in enumerate(lines[:60]):
        if not SDS_MARKER_RE.search(line):
            continue
        for candidate in lines[index + 1:index + 5]:
            candidate = _clean_candidate(candidate)
            if valid_chemical_name(candidate) and not re.search(r"版本|编号|编制|修订|第一部分", candidate):
                return {"chemical_name": candidate, "source": "standalone_identity_line", "confidence": 0.9}

    filename = clean_filename_title(source_file)
    filename = re.sub(r"(?:Microsoft Word\s*-\s*|GHS[_ -]?SDS[_ -]?\d*|ALC-SDS-[A-Z0-9_-]+|SDS|MSDS|Safety Data Sheet|安全资料表|化学品安全技术说明书)", " ", filename, flags=re.I)
    filename = re.sub(r"(?:-[0-9a-f]{8,}|\bPDF\b|\bDOCX?\b)", " ", filename, flags=re.I)
    filename = _clean_candidate(filename)
    if valid_chemical_name(filename) and len(filename) <= 50 and not re.search(r"www\.|tansoole|Microsoft", filename, re.I):
        return {"chemical_name": filename, "source": "cleaned_sds_filename", "confidence": 0.7}
    return {"chemical_name": "", "source": "identity_unresolved", "confidence": 0.0}


def recover_sds_title(text: str, source_file: str) -> dict[str, Any]:
    result = extract_sds_chemical_precise(text, source_file)
    chemical = result["chemical_name"]
    return {
        "title": f"{chemical} 化学品安全技术说明书" if chemical else "化学品安全技术说明书",
        "status": "valid" if chemical else "uncertain",
        "source": result["source"] if chemical else "generic_sds_title",
        "chemical_name": chemical,
        "confidence": result["confidence"],
        "issues": [] if chemical else ["sds_chemical_identity_unresolved"],
    }


def parent_title_issues(title: str, parent_type: str = "") -> list[str]:
    value = str(title or "").strip()
    issues: list[str] = []
    if not value:
        return ["missing"]
    if len(value) > 120:
        issues.append("overlong")
    if PURE_NUMBER_RE.fullmatch(value) or DATE_ONLY_RE.fullmatch(value):
        issues.append("pure_number_or_date")
    if HASH_RE.fullmatch(value):
        issues.append("hash_or_file_identifier")
    if FILEISH_RE.fullmatch(value) or re.search(r"[/\\](?:pdf|docx?|html?)?$", value, re.I):
        issues.append("filename_like")
    if BODY_START_RE.search(value) or (parent_type != "sds" and len(value) > 45 and re.search(r"[。；;，,]", value)):
        issues.append("body_sentence")
    if value.endswith(("，", ",", "；", ";", "：", ":", "。")):
        issues.append("trailing_sentence_punctuation")
    if PERSON_ROLE_RE.search(value):
        issues.append("personnel_or_role")
    if FIELD_STICK_RE.search(value):
        issues.append("field_or_page_stickiness")
    if re.match(r"^第[一二三四五六七八九十百0-9]+条\s+.+", value):
        issues.append("regulation_article_body")
    if parent_type == "sds" and (not value.endswith(("化学品安全技术说明书", "安全资料表（SDS）")) or FIELD_STICK_RE.search(value)):
        issues.append("invalid_sds_title")
    if parent_type in {"enterprise_plan", "enterprise_group_plan"}:
        if "%" in value:
            issues.append("filename_like")
        if re.search(r"(?:应急预案.{0,20}样本|应急预案材料$|预案汇编$|预案合集$|\.docx?$)", value, re.I):
            issues.append("filename_or_collection_label")
        if re.search(r"(?:X{2,}|x{2,}|某某|^集团股份有限公司)", value):
            issues.append("placeholder_or_generic_subject")
        if value.count("（") != value.count("）") or value.count("(") != value.count(")"):
            issues.append("unbalanced_parentheses")
        if re.fullmatch(r"(?:本|本次|该)?(?:生产安全事故|安全生产事故|突发环境事件)?(?:综合|专项)?应急预案", value):
            issues.append("enterprise_subject_missing")
        if GENERIC_ENTERPRISE_TITLE_RE.fullmatch(value) or value == "应急预案版本号":
            issues.append("enterprise_subject_missing")
        if not re.search(r"应急预案|现场处置方案|应急处置方案", value):
            issues.append("enterprise_plan_name_missing")
    return sorted(set(issues))


def _company_from_line(line: str) -> str:
    match = SPECIFIC_ENTERPRISE_RE.search(line)
    if not match:
        return ""
    value = re.sub(r"^(?:本预案为|本预案由|由)", "", match.group(0)).strip()
    if re.search(r"(?:X{2,}|x{2,}|某某)", value) or re.fullmatch(r"(?:集团)?股份有限公司|集团有限公司|有限公司", value):
        return ""
    if value.count("（") != value.count("）") or value.count("(") != value.count(")"):
        return ""
    return value


def _formal_title_from_line(line: str) -> str:
    value = html.unescape(re.sub(r"\s+", " ", line)).strip().strip("《》")
    embedded = re.search(r"《([^》]{3,115}(?:应急预案|现场处置方案|应急处置方案))》", value)
    if embedded:
        return embedded.group(1).strip()
    company_match = SPECIFIC_ENTERPRISE_RE.search(value)
    company = _company_from_line(value)
    plan = PLAN_TITLE_RE.search(value)
    if company and company_match and plan and company_match.start() <= plan.start():
        between = value[company_match.end():plan.start()]
        return f"{company}{between}{plan.group(0)}".strip(" -_：:")
    return ""


def recover_enterprise_title(parent: dict[str, Any], text: str) -> dict[str, Any]:
    existing = str(parent.get("parent_title") or "")
    lines = [line.strip() for line in normalize_text(text[:50000]).splitlines() if line.strip()]
    # Cover layouts often put the company and the plan name on adjacent lines.
    for index, line in enumerate(lines[:80]):
        company = _company_from_line(line)
        if not company:
            continue
        # Split cover titles use a standalone company line followed directly
        # by a short plan-name line.  Personnel rows and body paragraphs are
        # intentionally excluded from this adjacency rule.
        compact_line = re.sub(r"^(?:编制单位(?:名称)?\s*[:：]?\s*)", "", re.sub(r"\s+", "", line))
        compact_company = re.sub(r"\s+", "", company)
        if compact_line != compact_company:
            continue
        for nearby in lines[index:index + 3]:
            plan = PLAN_TITLE_RE.search(nearby)
            if not plan:
                continue
            if len(nearby) > 90 or re.search(r"[。；;，,]", nearby):
                continue
            suffix = nearby[plan.end():].strip()
            plan_name = plan.group(0) + (suffix if re.fullmatch(r"[（(](?:上册|下册|修订版|第\s*\d+\s*版)[）)]", suffix) else "")
            candidate = f"{company}{plan_name}"
            if not parent_title_issues(candidate, str(parent.get("parent_document_type") or "")):
                return {"title": candidate, "status": "valid", "source": "parsed_cover_company_plus_plan", "issues": []}

    # A complete company-and-plan title on the cover or title page has the
    # next priority.  Restrict this pass to the document front so that a later
    # directory entry or cited government plan cannot replace the file title.
    for line in lines[:80]:
        candidate = _formal_title_from_line(line)
        if candidate and not parent_title_issues(candidate, str(parent.get("parent_document_type") or "")):
            return {"title": candidate, "status": "valid", "source": "parsed_cover_or_title_line", "issues": []}

    compact_existing = re.sub(r"\s+", "", existing)
    compact_text = re.sub(r"\s+", "", text[:50000])
    if not parent_title_issues(existing, str(parent.get("parent_document_type") or "")) and compact_existing and compact_existing in compact_text:
        return {"title": existing, "status": "valid", "source": str(parent.get("parent_title_source") or "existing_valid_title"), "issues": []}

    # Last-resort document-title evidence.  This is intentionally after a
    # quality-checked existing title and remains conservative.
    for line in lines[80:180]:
        candidate = _formal_title_from_line(line)
        if candidate and not parent_title_issues(candidate, str(parent.get("parent_document_type") or "")):
            return {"title": candidate, "status": "valid", "source": "parsed_later_document_title", "issues": []}

    return {
        "title": "企业生产安全事故应急预案",
        "status": "uncertain",
        "source": "generic_enterprise_title_due_to_unresolved_subject",
        "issues": ["enterprise_formal_title_unresolved"],
    }


def reclassify_excluded(parent: dict[str, Any], text: str) -> dict[str, Any]:
    """Recheck only records previously excluded from the project corpus."""
    title = str(parent.get("parent_title") or "")
    source_file = str(parent.get("source_file") or "")
    label = f"{title}\n{clean_filename_title(source_file)}"
    normalized = normalize_text(text)
    if parent.get("parse_status") != "success" or len(normalized.strip()) < 80:
        return {"parent_document_type": "manual_review", "content_role": "manual_review", "confidence": 0.98, "evidence": ["parse_or_content_insufficient_not_irrelevant"], "requires_manual_review": True, "rag_enabled": False}

    sds = strong_sds_evidence(normalized, title, source_file)
    chemical = extract_sds_chemical_precise(normalized, source_file)["chemical_name"]
    traditional_sds_marker = bool(re.search(r"(?:物質)?安全資[料料]表", f"{label}\n{normalized[:20000]}"))
    if (sds["matched"] or (traditional_sds_marker and sds["section_count"] >= 4 and sds["identifier"])) and chemical:
        return {"parent_document_type": "sds", "content_role": "safety_data", "confidence": sds["confidence"], "evidence": ["strong_sds_structure", "specific_chemical_identity", f"sds_sections={sds['section_count']}"], "requires_manual_review": False, "rag_enabled": True}
    if SDS_MARKER_RE.search(f"{label}\n{normalized[:30000]}") and sds["section_count"] >= 4 and not chemical:
        return {"parent_document_type": "guidance_reference", "content_role": "sds_guidance_or_template", "confidence": 0.86, "evidence": ["sds_structure_without_specific_chemical_identity", "sds_guidance_or_template"], "requires_manual_review": True, "rag_enabled": True}

    accident = accident_evidence(normalized, title, source_file)
    collection_title = bool(ACCIDENT_COLLECTION_RE.search(label))
    accident_dates = len(re.findall(r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日|[“\"']?\d{1,2}[·.-]\d{1,2}[”\"']?", normalized[:100000]))
    accident_details = sum(bool(re.search(pattern, normalized[:100000])) for pattern in (r"事故经过", r"人员伤亡|死亡|造成[\s\S]{0,20}人", r"事故原因|发生原因|原因分析", r"责任追究|事故责任", r"整改措施|防范措施|事故教训|主要教训"))
    if accident["matched"] or (collection_title and (accident_dates >= 1 or accident_details >= 2)):
        return {"parent_document_type": "accident_case", "content_role": "accident_case_collection" if collection_title else "accident_case_reference", "confidence": 0.94 if accident["matched"] else 0.88, "evidence": ["explicit_accident_case_or_report_title", f"accident_date_hits={accident_dates}", f"accident_detail_groups={accident_details}"], "requires_manual_review": False, "rag_enabled": True}

    compact_label = re.sub(r"\s+", "", label)
    structure_count, structure_names = plan_structure_evidence(normalized)
    if GOV_TITLE_RE.search(label) and (GOV_RE.search(f"{label}\n{normalized[:12000]}") or structure_count >= 2):
        return {"parent_document_type": "government_or_regional_plan", "content_role": "emergency_plan", "confidence": 0.9, "evidence": ["government_plan_title", f"plan_structure_groups={structure_count}", *structure_names], "requires_manual_review": False, "rag_enabled": True}
    if re.search(r"(?:大学|学院|学校|医院).{0,50}(?:应急预案|处置方案)", label) and structure_count >= 1:
        return {"parent_document_type": "organization_plan", "content_role": "emergency_plan", "confidence": 0.86, "evidence": ["organization_plan_title", f"plan_structure_groups={structure_count}"], "requires_manual_review": True, "rag_enabled": True}

    semantic = classify_parent_semantic({**parent, "parent_document_type": "excluded_irrelevant"}, normalized)
    if semantic["parent_document_type"] not in {"excluded_irrelevant", "manual_review", "chemical_reference"}:
        return semantic

    file_title = clean_filename_title(source_file)
    if FORMAL_REGULATION_RE.search(file_title) and FORMAL_RELATED_RE.search(f"{file_title}\n{normalized[:20000]}") and not GUIDANCE_TITLE_RE.search(file_title):
        return {"parent_document_type": "regulation", "content_role": "regulatory_reference", "confidence": 0.86, "evidence": ["project_related_formal_regulation_title"], "requires_manual_review": True, "rag_enabled": True}
    if GUIDANCE_TITLE_RE.search(file_title) and FORMAL_RELATED_RE.search(f"{file_title}\n{normalized[:20000]}"):
        return {"parent_document_type": "guidance_reference", "content_role": "guidance_reference", "confidence": 0.86, "evidence": ["project_related_guidance_title"], "requires_manual_review": False, "rag_enabled": True}
    if FORMAL_RELATED_RE.search(f"{file_title}\n{normalized[:20000]}"):
        return {"parent_document_type": "manual_review", "content_role": "manual_review", "confidence": 0.6, "evidence": ["project_relevant_but_type_uncertain"], "requires_manual_review": True, "rag_enabled": False}
    if IRRELEVANT_RE.search(f"{compact_label}\n{normalized[:5000]}"):
        return {"parent_document_type": "excluded_irrelevant", "content_role": "excluded_irrelevant", "confidence": 0.96, "evidence": ["explicit_navigation_or_error_page"], "requires_manual_review": False, "rag_enabled": False}
    return {"parent_document_type": "excluded_irrelevant", "content_role": "excluded_irrelevant", "confidence": 0.85, "evidence": ["no_project_relevant_evidence_after_full_recheck"], "requires_manual_review": False, "rag_enabled": False}


def _clean_section_heading(value: str) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    # Remove punctuation left by English enumerated headings, but never invent
    # words that are not already present in the source boundary.
    title = title.rstrip(" ,，;；")
    glued = re.match(r"^((?:\d+(?:\.\d+){0,4}|第[一二三四五六七八九十百0-9]+[章节部分])\s*(?:救援注意事项|现场处置要点|应急处置措施|事故风险分析|组织机构与职责|应急响应|应急保障))(?=一旦|事故发生|作业现场|负责|立即)", title)
    if glued:
        title = glued.group(1)
    return title.strip()


def audit_section_title(segment: dict[str, Any]) -> dict[str, Any]:
    original = str(segment.get("section_title") or "").strip()
    segment_type = str(segment.get("segment_type") or "")
    content = str(segment.get("content") or segment.get("raw_text") or "")
    origin = str(segment.get("source_origin") or "")
    if segment_type == "full_document":
        return {"section_title": None, "valid": False, "source": "not_applicable_full_document", "confidence": 1.0, "issues": ["full_document_uses_parent_title"] if original else []}
    if not original:
        return {"section_title": None, "valid": False, "source": "missing", "confidence": 0.0, "issues": ["no_section_title"]}

    title = _clean_section_heading(original)
    issues: list[str] = []
    if len(title) > 80:
        issues.append("overlong")
    if SECTION_DATE_ONLY_RE.fullmatch(title) or SECTION_DATE_SENTENCE_RE.search(title):
        # A compact accident-case heading may legitimately contain a date.
        if not (segment_type == "accident_case_section" and "事故" in title and len(title) <= 70 and not re.search(r"[，,。]", title)):
            issues.append("date_or_accident_narrative")
    if SECTION_NUMERIC_RE.fullmatch(title):
        issues.append("numeric_or_table_value")
    if PERSON_ROLE_RE.search(title):
        issues.append("personnel_description")
    if SECTION_ACTION_RE.search(title):
        issues.append("action_or_body_sentence")
    if title.endswith(("。", "：", ":")) or (len(title) > 35 and re.search(r"[，,；;]", title)):
        issues.append("sentence_punctuation")
    if re.search(r"\d+\s*/\s*\d+|\.{4,}\s*\d+$", title):
        issues.append("page_or_toc_fragment")
    if segment_type in {"front_matter", "metadata_section", "personnel_table", "contact_list", "reference_sentence", "list_fragment", "toc_fragment"}:
        issues.append("context_fragment_not_section_heading")

    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    boundary_match = bool(first_line and (_clean_section_heading(first_line) == title or first_line.startswith(original)))
    trusted_origin = origin.startswith("generated_structural_section") or origin.startswith("semantic_html_section") or origin in {"parsed_heading", "extracted_section"}
    heading_shape = bool(SECTION_HEADING_RE.search(title)) or bool(re.match(r"^(?:\d+(?:\.\d+){0,4}|第[一二三四五六七八九十百0-9]+[章节部分])\s*[\u3400-\u9fffA-Za-z]", title))
    if not boundary_match:
        issues.append("not_at_local_heading_boundary")
    if not heading_shape:
        issues.append("not_heading_shaped")
    if not trusted_origin and not boundary_match:
        issues.append("untrusted_title_source")

    if issues:
        return {"section_title": None, "valid": False, "source": "rejected_non_heading_text", "confidence": 0.0, "issues": sorted(set(issues)), "original_title": original}
    return {
        "section_title": title,
        "valid": True,
        "source": "parsed_heading_boundary" if trusted_origin else "local_text_heading_boundary",
        "confidence": 0.98 if trusted_origin else 0.88,
        "issues": [],
        "original_title": original,
    }
