"""Semantic classification and content-quality rules for the final repair.

Production decisions only inspect source evidence. Regression identifiers are
intentionally absent from this module.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

from .normalization import normalize_text


SDS_SECTIONS = {
    "identity": r"化学品(?:及企业)?标识|化学品及企业信息|化学品中文名称|化學品與廠商資料|物品與廠商資料|product (?:and company )?identification|identification of the substance",
    "hazards": r"危险性概述|危害概述|健康危害|危害辨识|危害辨識|hazards? identification",
    "composition": r"成分.?组成信息|主要组成与性状|组分信息|組成.?成分|composition.?information on ingredients",
    "first_aid": r"急救措施|急救人|急救中心|first.?aid measures",
    "fire": r"消防措施|灭火措施|滅火措施|灭火注意|燃爆特性与消防|firefighting measures|fire.?fighting measures",
    "spill": r"泄漏应急处理|泄漏处置|泄漏化学品|切断泄漏源|洩漏處理方法|accidental release measures",
    "handling": r"操作处置与储存|操作和储存|储存注意事项|储运注意事项|安全處置與儲存方法|handling and storage",
    "exposure": r"接触控制.?个体防护|防护措施|职业暴露限值|暴露限值|暴露控制.?個人防護|暴露預防措施|exposure controls?.?personal protection",
    "properties": r"理化特性|理化性质|物理状态|物理及化學性質|physical and chemical properties",
    "stability": r"稳定性和反应性|安定性及反應性|stability and reactivity",
    "toxicology": r"毒理学信息|急性毒性|毒性資料|toxicological information",
    "ecology": r"生态学信息|环境资料|生態資料|ecological information",
    "disposal": r"废弃处置|廢棄處置方法|disposal considerations",
    "transport": r"运输信息|联合国运输名称|運送資料|transport information",
    "regulatory": r"法规信息|法規資料|regulatory information",
    "other": r"其他信息|other information",
}
STRICT_SDS_SECTIONS = {
    "identity": r"化学品(?:及企业)?标识|化學品與廠商資料|product (?:and company )?identification|identification of the substance",
    "hazards": r"危险性概述|危害概述|危害辨识|危害辨識|hazards? identification",
    "composition": r"成分.?组成信息|组分信息|組成.?成分|composition.?information on ingredients",
    "first_aid": r"急救措施|first.?aid measures",
    "fire": r"消防措施|灭火措施|滅火措施|fire.?fighting measures",
    "spill": r"泄漏应急处理|泄漏处置|洩漏處理方法|accidental release measures",
    "handling": r"操作处置与储存|操作和储存|安全處置與儲存方法|handling and storage",
    "exposure": r"接触控制.?个体防护|暴露控制.?個人防護|暴露預防措施|exposure controls?.?personal protection",
    "properties": r"理化特性|物理及化學性質|physical and chemical properties",
    "stability": r"稳定性和反应性|安定性及反應性|stability and reactivity",
    "toxicology": r"毒理学信息|毒性資料|toxicological information",
    "ecology": r"生态学信息|生態資料|ecological information",
    "disposal": r"废弃处置|廢棄處置方法|disposal considerations",
    "transport": r"运输信息|運送資料|transport information",
    "regulatory": r"法规信息|法規資料|regulatory information",
    "other": r"其他信息|other information",
}
PLAN_STRUCTURES = {
    "risk": r"事故风险分析|风险分析|危险性分析|危险有害因素",
    "organization": r"组织机构(?:和|与)?职责|应急组织|指挥机构",
    "response": r"应急响应|响应分级|响应程序|预警与信息报告",
    "disposal": r"应急处置|处置措施|现场处置|处置方案",
    "support": r"应急保障|保障措施|物资保障|通信保障",
}
SDS_MARKER_RE = re.compile(r"Safety Data Sheet|\bM?SDS\b|安全资料表|安全資料表|物質安全資料表|化\s*学\s*品\s*安\s*全\s*技\s*术\s*说\s*明\s*书|安全技术说明书", re.I)
SDS_ID_RE = re.compile(r"化学品(?:中文|英文)?名称|化學品名稱|产品名称|Product (?:name|identifier)|Chemical name|\bName\s*[:：]", re.I)
SDS_NUMBER_RE = re.compile(r"\bCAS(?:\s*(?:No\.?|号|號))?\s*[:：]?[\s\S]{0,100}?\d{2,7}-\d{2}-\d\b|\bUN\s*(?:No\.?)?\s*[:：]?[\s\S]{0,40}?\d{4}\b|\bEC\s*(?:No\.?)?\s*[:：]?[\s\S]{0,40}?\d{3}-\d{3}-\d\b", re.I)
REG_FORMAL_RE = re.compile(r"(?:中华人民共和国.+法|条例|管理办法|规定|规程|标准|规范|编制导则|实施细则|管理规则)$")
ACCIDENT_TITLE_RE = re.compile(r"事故调查报告|事故调查处理报告|事故通报|事故案例|事故整改(?:措施落实情况)?评估报告|事故防范和整改措施落实情况评估|事故评估报告|盲目施救导致伤亡")
GOV_RE = re.compile(r"人民政府|管委会|安委会|应急管理(?:局|厅|部)|街道办事处|镇人民政府|乡人民政府")
GOV_TITLE_RE = re.compile(r"(?:省|市|区|县|镇|乡|街道|开发区|园区).{0,45}(?:应急预案|救援预案)")
ENTERPRISE_NAME_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,60}(?:集团)?(?:有限责任公司|股份有限公司|有限公司|集团公司|公司|工厂|厂|煤矿|矿业|矿|电厂|气站|油库)")
ORGANIZATION_RE = re.compile(r"(?:大学|学院|学校|医院|研究院|事业单位).{0,40}(?:应急预案|处置方案)")
GUIDANCE_RE = re.compile(r"指南|指导意见|指导手册|操作手册|实施解读|标准解读|应急处置原则|安全措施和应急处置原则")
IRRELEVANT_RE = re.compile(r"政府信息公开指南|网站导航|新闻列表|用户登录|登录注册|404\s*not found|页面不存在|下载错误", re.I)
SCRIPT_RE = re.compile(r"WebFXTreeItem|javascript:|function\s*\(|(?:^|\s)(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=|document\.(?:write|getElementById)|window\.|addEventListener|\.prototype\.", re.I)
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]{0,300}>")
NAV_WORD_RE = re.compile(r"首页|网站首页|当前位置|面包屑|网站地图|主办单位|版权所有|相关推荐|推荐阅读|登录|注册|栏目导航|返回顶部")
NAVIGATION_CONTROL_RE = re.compile(r"首页|网站首页|当前位置|面包屑|网站地图|栏目导航|返回顶部|上一页|下一页|版权所有|用户登录|登录注册")
PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-—－ ]?\d{7,8})(?!\d)")
DATE_RE = re.compile(r"(?:19|20)\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?|(?:编制|发布|修订|实施)日期")
PAGE_RE = re.compile(r"^\s*(?:第?\s*\d+\s*页(?:/共\s*\d+\s*页)?|\d+\s*/\s*\d+)\s*$", re.I)
ARTICLE_TITLE_RE = re.compile(r"^\s*第[一二三四五六七八九十百零〇0-9]+条(?:\s+|$)")
ACTION_TITLE_RE = re.compile(r"^(?:负责|组织|建议|事故发生|发生事故|造成|需要|立即|按照|根据|开展|采取|确认|编制|发布|修订|对下列|第一条\s+为|第二条\s+)")


def compact_cjk(text: str) -> str:
    previous = text
    for _ in range(3):
        current = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", previous)
        if current == previous:
            break
        previous = current
    return previous


def count_evidence(text: str, patterns: dict[str, str]) -> tuple[int, list[str]]:
    matched = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
    return len(matched), matched


def strong_sds_evidence(text: str, title: str = "", source_file: str = "") -> dict[str, Any]:
    combined = compact_cjk(f"{title}\n{Path(source_file).stem}\n{text}")
    marker = bool(SDS_MARKER_RE.search(combined[:20000]))
    identity = bool(SDS_ID_RE.search(combined[:25000]))
    identifier = bool(SDS_NUMBER_RE.search(combined[:50000]))
    section_count, section_names = count_evidence(combined[:150000], SDS_SECTIONS)
    strict_section_count, strict_section_names = count_evidence(combined[:150000], STRICT_SDS_SECTIONS)
    group_a = marker and identity and identifier and section_count >= 4
    group_b = strict_section_count >= 8
    return {
        "matched": group_a or group_b,
        "group_a": group_a,
        "group_b": group_b,
        "marker": marker,
        "identity": identity,
        "identifier": identifier,
        "section_count": section_count,
        "sections": section_names,
        "strict_section_count": strict_section_count,
        "strict_sections": strict_section_names,
        "confidence": round(min(0.99, 0.72 + section_count * 0.025 + marker * 0.04 + identity * 0.03 + identifier * 0.03), 3) if group_a or group_b else 0.0,
    }


def is_pubchem(source_file: str, title: str, text: str = "") -> bool:
    return bool(re.search(r"PubChem", f"{source_file}\n{title}\n{text[:500]}", re.I))


def accident_evidence(text: str, title: str, source_file: str) -> dict[str, Any]:
    label = f"{title}\n{Path(source_file).stem}"
    explicit = bool(ACCIDENT_TITLE_RE.search(label))
    plan_or_disposal = bool(re.search(r"应急预案|现场处置方案|现场处置要点", label)) and not explicit
    combined = f"{label}\n{text[:80000]}"
    date = bool(re.search(r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日|[“\"']?\d{1,2}[·.-]\d{1,2}[”\"']?\s*(?:事故|较大|重大|一般)", combined))
    entity = bool(re.search(r"(?:有限公司|公司|项目|矿|厂|市|区|县).{0,40}(?:发生|事故)", combined))
    details = sum(bool(re.search(pattern, combined)) for pattern in (r"事故经过", r"人员伤亡|造成\s*\d+\s*人|死亡\s*\d+\s*人", r"事故原因|原因分析", r"责任追究|事故责任", r"整改措施|防范措施|整改评估"))
    matched = not plan_or_disposal and explicit and details >= 2
    return {"matched": matched, "explicit_title": explicit, "date": date, "entity": entity, "detail_groups": details, "plan_or_disposal": plan_or_disposal}


def plan_structure_evidence(text: str) -> tuple[int, list[str]]:
    return count_evidence(compact_cjk(text[:150000]), PLAN_STRUCTURES)


def classify_parent_semantic(parent: dict[str, Any], text: str) -> dict[str, Any]:
    """Apply the requested strong-evidence priority without identifier rules."""
    title = str(parent.get("parent_title") or parent.get("canonical_title") or "")
    source_file = str(parent.get("source_file") or "")
    old_type = str(parent.get("parent_document_type") or "")
    file_title = clean_filename_title(source_file)
    # The repaired parent title is useful evidence only when it is not an
    # already-known body fragment.  The physical filename and first document
    # lines are deliberately kept separate from arbitrary body mentions.
    reliable_existing_title = title if not invalid_parent_title(title) else ""
    label = f"{file_title}\n{reliable_existing_title}"
    if parent.get("parse_status") != "success" or len(normalize_text(text)) < 80:
        return {"parent_document_type": "manual_review", "confidence": 0.95, "evidence": ["parse_or_content_insufficient"], "requires_manual_review": True}
    if is_pubchem(source_file, title, text):
        return {"parent_document_type": "chemical_reference", "confidence": 1.0, "evidence": ["pubchem_source_artifact"], "requires_manual_review": False, "content_role": "chemical_reference", "rag_enabled": False}

    sds = strong_sds_evidence(text, title, source_file)
    if sds["matched"]:
        return {"parent_document_type": "sds", "confidence": sds["confidence"], "evidence": ["sds_strong_structure", f"sds_group_a={sds['group_a']}", f"sds_group_b={sds['group_b']}", f"sds_sections={sds['section_count']}", *sds["sections"]], "requires_manual_review": False, "content_role": "safety_data", "rag_enabled": True}
    if old_type == "sds" and sds["marker"] and sds["section_count"] >= 3:
        return {"parent_document_type": "sds", "confidence": 0.7, "evidence": ["prior_sds_with_partial_structure", f"sds_sections={sds['section_count']}", *sds["sections"]], "requires_manual_review": True, "content_role": "safety_data", "rag_enabled": True}

    compact = compact_cjk(text)
    nonempty_lines = [line.strip() for line in compact.splitlines() if line.strip()]
    first_lines = "\n".join(nonempty_lines[:20])
    short_heading_lines = [line for line in nonempty_lines[:30] if 2 <= len(line) <= 120]
    document_heading_scope = "\n".join(short_heading_lines[:12])
    formal_label = f"{file_title}\n{document_heading_scope}"
    if old_type == "regulation" and reliable_existing_title:
        formal_label = f"{formal_label}\n{reliable_existing_title}"
    article_count = len(re.findall(r"第[一二三四五六七八九十百零〇0-9]+条", compact[:100000]))
    standard = bool(re.search(r"GB(?:/T)?\s*29639|生产经营单位生产安全事故应急预案编制导则|国家标准\s*GB|行业标准\s*[A-Z]", compact_cjk(file_title), re.I))
    formal_reg = bool(re.search(r"(?:中华人民共和国[^\n]{1,50}法|[^\n]{2,100}(?:条例|办法|规定|规程|标准|规范|编制导则|实施细则|管理规则))(?=\s|$|（|\(|--|-现行|-修订|-修正)", formal_label))
    guidance_title = bool(GUIDANCE_RE.search(file_title))
    accident_title = bool(ACCIDENT_TITLE_RE.search(file_title))
    if not guidance_title and not accident_title and (standard or (formal_reg and (article_count >= 3 or old_type == "regulation"))):
        return {"parent_document_type": "regulation", "confidence": 0.96 if standard or article_count >= 5 else 0.88, "evidence": [f"formal_regulation={formal_reg}", f"article_count={article_count}", f"standard_or_compilation_guide={standard}"], "requires_manual_review": False, "content_role": "regulatory_reference", "rag_enabled": True}

    accident = accident_evidence(text, title, source_file)
    if accident["matched"]:
        return {"parent_document_type": "accident_case", "confidence": min(0.97, 0.70 + accident["detail_groups"] * 0.05), "evidence": [f"accident_explicit_title={accident['explicit_title']}", f"accident_date={accident['date']}", f"accident_entity={accident['entity']}", f"accident_detail_groups={accident['detail_groups']}"], "requires_manual_review": False, "content_role": "accident_case_reference", "rag_enabled": True}

    gov_title = bool(GOV_TITLE_RE.search(f"{label}\n{document_heading_scope}"))
    gov_issuer = bool(GOV_RE.search(f"{label}\n{first_lines[:12000]}"))
    gov_scope = bool(re.search(r"本省|全省|本市|全市|本区|全区|本县|辖区|成员单位职责|区域资源调度", compact[:30000]))
    if (gov_title and gov_issuer) or (old_type == "government_or_regional_plan" and gov_issuer and gov_scope and plan_structure_evidence(text)[0] >= 2):
        return {"parent_document_type": "government_or_regional_plan", "confidence": 0.95 if gov_issuer and gov_scope else 0.88, "evidence": [f"government_title={gov_title}", f"government_issuer={gov_issuer}", f"jurisdiction_scope={gov_scope}"], "requires_manual_review": False, "content_role": "emergency_plan", "rag_enabled": True}

    structures, structure_names = plan_structure_evidence(text)
    company_matches = ENTERPRISE_NAME_RE.findall(f"{label}\n{first_lines[:10000]}")
    company_subject = bool(company_matches)
    internal = bool(re.search(r"本公司|本企业|我公司|本厂|本矿|厂区|矿区|公司内部|本单位|适用于.{0,30}(?:公司|厂|矿)", compact[:40000]))
    plan_title = bool(re.search(r"应急预案|现场处置方案|应急处置方案", f"{label}\n{document_heading_scope}"))
    excluded_kind = bool(re.search(r"编制导则|GB/T\s*29639|指南|指导意见|事故调查报告|Safety Data Sheet|\bM?SDS\b", compact_cjk(file_title), re.I))
    if company_subject and internal and structures >= 3 and plan_title and not excluded_kind:
        group_subject = bool(re.search(r"(?:集团有限公司|集团公司|集团).{0,35}(?:应急预案|现场处置方案)", label))
        ptype = "enterprise_group_plan" if group_subject else "enterprise_plan"
        return {"parent_document_type": ptype, "confidence": min(0.97, 0.78 + structures * 0.035), "evidence": ["specific_enterprise_subject", "enterprise_internal_scope", f"plan_structure_groups={structures}", *structure_names], "requires_manual_review": False, "content_role": "emergency_plan", "rag_enabled": True}

    if ORGANIZATION_RE.search(f"{label}\n{document_heading_scope}") and structures >= 3:
        return {"parent_document_type": "organization_plan", "confidence": 0.9, "evidence": ["non_enterprise_organization_plan", f"plan_structure_groups={structures}"], "requires_manual_review": False, "content_role": "emergency_plan", "rag_enabled": True}
    if re.search(r"危险化学品目录|危化品目录|化学品名录|危险化学品名录|目录指南", f"{label}\n{document_heading_scope}"):
        return {"parent_document_type": "chemical_catalog", "confidence": 0.95, "evidence": ["chemical_catalog_title"], "requires_manual_review": False, "content_role": "chemical_catalog", "rag_enabled": True}
    if GUIDANCE_RE.search(f"{file_title}\n{document_heading_scope}") and re.search(r"应急|安全生产|危险化学品|有限空间|事故|泄漏|中毒|窒息", compact[:30000]):
        return {"parent_document_type": "guidance_reference", "confidence": 0.92, "evidence": ["project_relevant_guidance_title"], "requires_manual_review": False, "content_role": "guidance_reference", "rag_enabled": True}
    if IRRELEVANT_RE.search(f"{file_title}\n{document_heading_scope}"):
        return {"parent_document_type": "excluded_irrelevant", "confidence": 0.95, "evidence": ["irrelevant_navigation_or_error_page"], "requires_manual_review": False, "content_role": "excluded_irrelevant", "rag_enabled": False}
    if plan_title and structures >= 3:
        return {"parent_document_type": "manual_review", "confidence": 0.58, "evidence": ["plan_structure_present_but_subject_uncertain", f"plan_structure_groups={structures}"], "requires_manual_review": True, "content_role": "manual_review", "rag_enabled": False}
    # Preserve only a still-supported low-priority type.  This avoids turning
    # an uncertain page into a more specific class merely because its body
    # quotes common project vocabulary.
    if old_type == "guidance_reference" and re.search(r"应急|安全生产|危险化学品|有限空间|事故处置|泄漏", compact[:30000]):
        return {"parent_document_type": "guidance_reference", "confidence": 0.62, "evidence": ["project_relevant_reference_content"], "requires_manual_review": True, "content_role": "guidance_reference", "rag_enabled": True}
    return {"parent_document_type": "excluded_irrelevant", "confidence": 0.8, "evidence": ["no_project_relevant_semantic_structure"], "requires_manual_review": False, "content_role": "excluded_irrelevant", "rag_enabled": False}


def clean_filename_title(source_file: str) -> str:
    value = unquote(Path(source_file).stem)
    value = re.sub(r"-[0-9a-f]{10,}$", "", value, flags=re.I)
    value = re.sub(r"^(?:PDF|DOCX?|HTML)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+-\s+(?:[^-]{2,80}\.(?:com|cn|org|net|gov)(?:\.cn)?)$", "", value, flags=re.I)
    return value.strip(" _-｜|")


def invalid_parent_title(title: str) -> list[str]:
    value = title.strip()
    issues: list[str] = []
    if not value:
        return ["missing"]
    if len(value) > 120: issues.append("overlong")
    if PAGE_RE.match(value) or re.match(r"^\d+\s*/\s*\d+", value): issues.append("page_number")
    if ARTICLE_TITLE_RE.match(value): issues.append("regulation_article_body")
    if ACTION_TITLE_RE.match(value): issues.append("sentence_or_action")
    if DATE_RE.fullmatch(value): issues.append("date_only")
    if SCRIPT_RE.search(value) or HTML_TAG_RE.search(value): issues.append("script_or_html")
    if re.search(r"(?:急救措施|消防措施|供应商详细信息|化学品与厂商资料)$", value): issues.append("section_title_as_parent")
    if value.endswith(("：", ":", "；", ";", "。")): issues.append("sentence_punctuation")
    return issues


def extract_sds_chemical(text: str, source_file: str) -> str:
    compact = compact_cjk(text[:30000])
    patterns = (
        r"化学品中文名称\s*[:：]\s*([^\n|;；]{1,60})",
        r"产品中文名称\s*(?:[:：]\s*|\s+)([^\n|;；]{1,60})",
        r"化学品名称\s*[:：]\s*([^\n|;；]{1,60})",
        r"化学品名称\s+([^\n|;；]{1,60})",
        r"Product name\s*[:：]\s*([^\n|;；]{1,80})",
        r"Product identifier\s*[:：]\s*([^\n|;；]{1,80})",
        r"(?:SAFETY DATA SHEET|Safety Data Sheet)\s*[|\n ]+([^\n|]{2,70})",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" :：;；")
            if not re.search(r"供应商|supplier|SDS No|企业|^\d+\s*/\s*\d+$|安全技术说明书$", value, re.I) and len(value) <= 80:
                return value
    # Many Chinese SDS layouts put the product identity on the line directly
    # after the document title rather than after a colon.
    lines = [line.strip() for line in compact.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if SDS_MARKER_RE.search(line):
            for candidate in lines[index + 1:index + 4]:
                candidate = re.sub(r"\s+", " ", candidate).strip()
                if 1 < len(candidate) <= 60 and not invalid_parent_title(candidate) and not re.search(r"版本|报告编号|SDS No|\d+\s*/\s*\d+", candidate, re.I):
                    return candidate
    filename = clean_filename_title(source_file)
    filename = re.sub(r"\b(?:SDS|MSDS|Safety Data Sheet|安全资料表|化学品安全技术说明书)\b", "", filename, flags=re.I).strip(" _-()（）")
    if filename and not re.search(r"ALC-SDS|Thermo Fisher|Microsoft Word|www\.", filename, re.I) and len(filename) <= 70:
        return filename
    return ""


def recover_parent_title_semantic(parent: dict[str, Any], text: str, html_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    ptype = parent["parent_document_type"]
    source_file = parent["source_file"]
    if ptype == "chemical_reference":
        title = clean_filename_title(source_file)
        return {"title": title, "status": "valid" if title else "missing", "source": "pubchem_filename", "issues": [] if title else ["missing"]}
    if ptype == "sds":
        chemical = extract_sds_chemical(text, source_file)
        title = f"{chemical} 化学品安全技术说明书" if chemical else "化学品安全技术说明书"
        return {"title": title, "status": "valid" if chemical else "uncertain", "source": "sds_chemical_identity" if chemical else "generic_sds_title", "issues": [] if chemical else ["chemical_identity_uncertain"]}

    candidates: list[tuple[str, str]] = []
    if html_meta:
        candidates.extend([("html_title", str(html_meta.get("title") or "")), ("html_h1", str(html_meta.get("h1") or ""))])
    filename = clean_filename_title(source_file)
    candidates.append(("cleaned_filename", filename))
    candidates.append(("existing_parent_title", str(parent.get("parent_title") or "")))
    for line in [line.strip().strip("《》") for line in text.splitlines()[:100] if line.strip()]:
        if re.search(r"应急预案|现场处置方案|事故调查报告|事故评估报告|规程|规定|办法|条例|标准|导则|指南|指导意见", line):
            candidates.append(("document_first_level_title", line))
    for source, candidate in candidates:
        candidate = html.unescape(re.sub(r"\s+", " ", candidate)).strip().strip("《》")
        embedded = re.search(r"《([^》]{2,110}(?:应急预案|现场处置方案|调查报告|评估报告|规程|规定|办法|条例|指南|指导意见))》", candidate)
        if embedded:
            candidate = embedded.group(1)
        issues = invalid_parent_title(candidate)
        if not issues:
            return {"title": candidate, "status": "valid", "source": source, "issues": []}
    return {"title": "", "status": "missing", "source": "missing", "issues": ["no_reliable_document_title"]}


def clean_html_semantic(raw: bytes) -> dict[str, Any]:
    match = from_bytes(raw).best()
    encoding = match.encoding if match else "utf_8"
    decoded = str(match) if match else raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.h1.get_text(" ", strip=True) if soup.h1 else ""
    selectors = "script,style,noscript,svg,nav,footer,header,aside,form,menu,template,iframe,.nav,.navbar,.footer,.header,.sidebar,.menu,.breadcrumb,.recommend,.related,.login,.register,#nav,#footer,#header,#sidebar,#menu"
    for node in soup.select(selectors):
        node.decompose()
    candidates = soup.select("article,main,[role=main],.article,.article-content,.content,.news-content,.TRS_Editor")
    body = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup.body or soup)
    lines: list[str] = []
    script_lines = 0
    nav_lines = 0
    for raw_line in body.get_text("\n", strip=True).splitlines():
        line = html.unescape(re.sub(r"\s+", " ", raw_line)).strip()
        if not line:
            continue
        if SCRIPT_RE.search(line):
            script_lines += 1
            continue
        if NAV_WORD_RE.search(line) and len(line) <= 100:
            nav_lines += 1
            continue
        if re.fullmatch(r"(?:首页|新闻|政策|法规|下载|联系我们|返回|上一页|下一页|打印|关闭)(?:\s*[>|/·-]\s*)*", line):
            nav_lines += 1
            continue
        lines.append(line)
    text = normalize_text("\n".join(lines))
    js_hits = len(SCRIPT_RE.findall(text))
    tag_hits = len(HTML_TAG_RE.findall(text))
    nav_hits = len(NAV_WORD_RE.findall(text))
    page_type = "content"
    if re.search(r"404\s*not found|页面不存在", f"{title}\n{text[:3000]}", re.I): page_type = "404"
    elif re.search(r"用户登录|账号登录|请输入密码|登录系统", f"{title}\n{text[:3000]}"): page_type = "login_page"
    elif len(text) < 120: page_type = "empty_shell"
    failed = page_type != "content" or js_hits > 0 or tag_hits > 0 or (nav_hits > 15 and nav_hits * 20 > len(text))
    return {
        "parser": "html_semantic_cleaner", "encoding": encoding, "title": title, "h1": h1,
        "raw_text": text, "normalized_text": text, "text_length": len(text),
        "parse_status": "html_content_extraction_failed" if failed else "success",
        "html_page_type": page_type, "javascript_hits": js_hits, "html_tag_hits": tag_hits,
        "navigation_hits": nav_hits, "removed_script_lines": script_lines,
        "removed_navigation_lines": nav_lines,
    }


def has_navigation_or_script_pollution(text: str) -> bool:
    """Detect actual page chrome/code without flagging legal uses of “注册”.

    A single ordinary word is not navigation evidence.  Navigation pollution
    requires code/markup or several independent browser-control labels.
    """
    if SCRIPT_RE.search(text) or HTML_TAG_RE.search(text):
        return True
    labels = NAVIGATION_CONTROL_RE.findall(text)
    return len(labels) >= 3 and len(set(labels)) >= 2


def classify_segment_semantic(segment: dict[str, Any], parent_type: str) -> tuple[str, list[str]]:
    text = normalize_text(str(segment.get("content") or segment.get("raw_text") or ""))
    title = str(segment.get("section_title") or segment.get("title") or "").strip()
    old = str(segment.get("segment_type") or "")
    warnings: list[str] = []
    if segment.get("is_parent_full_document"):
        return "full_document", warnings
    if old == "toc_fragment" or re.search(r"\.{4,}\s*\d+\s*$", text, re.M):
        return "toc_fragment", warnings
    if old in {"reference_sentence", "list_fragment"} and len(text) < 250:
        return old, warnings
    structure_count, _ = plan_structure_evidence(text)
    phone_count = len(PHONE_RE.findall(text))
    name_role_hits = len(re.findall(r"(?:总指挥|副总指挥|组长|副组长|成员|联系人|姓名|职务)\s*[:：\t ]", text))
    date_hits = len(DATE_RE.findall(text))
    substantive = structure_count >= 2 or bool(re.search(r"应急处置|事故经过|原因分析|急救措施|泄漏应急处理", text))
    if phone_count >= 3 and not substantive:
        return "contact_list", ["excluded_contact_list"]
    if name_role_hits >= 3 and not substantive:
        return "personnel_table", ["excluded_personnel_table"]
    if len(text) <= 1800 and not substantive and (date_hits >= 2 or re.search(r"(?:编制单位|批准人|审核人|版本号|预案编号|发布令|签字栏)", text)):
        return "front_matter", ["excluded_front_matter"]
    if len(text) <= 1000 and not substantive and re.search(r"(?:License|SourceID|Reference|Revision|版本信息|元数据)", text, re.I):
        return "metadata_section", ["excluded_metadata_section"]
    if parent_type == "accident_case":
        return "accident_case_section", warnings
    if old == "accident_case_section":
        warnings.append("accident_case_section_removed_from_non_case_parent")
    if parent_type == "sds":
        return "sds_section", warnings
    if parent_type == "regulation":
        return "regulation_article", warnings
    if re.search(r"现场处置方案|现场处置要点|应急处置要点", title) or (re.search(r"现场处置方案|现场处置要点", text[:500]) and len(text) >= 120):
        return "onsite_disposal_plan", warnings
    if re.search(r"专项应急预案|事故应急预案", title) and structure_count >= 2:
        return "embedded_special_section", warnings
    if old == "table" and not text.strip():
        return "metadata_section", ["excluded_empty_table"]
    if old in {"table", "appendix_sds", "embedded_sds"}:
        return old, warnings
    return "guidance_section" if len(text) >= 120 else "list_fragment", warnings


def segment_rag_enabled(segment: dict[str, Any], parent: dict[str, Any]) -> bool:
    if not parent.get("rag_enabled", True): return False
    if parent.get("parse_status") != "success": return False
    if not segment.get("is_canonical", True): return False
    if segment.get("source_origin") == "source_record_repaired": return False
    if segment.get("segment_type") in {"front_matter", "metadata_section", "personnel_table", "contact_list", "toc_fragment", "reference_sentence", "list_fragment"}: return False
    text = str(segment.get("content") or "")
    if len(text.strip()) < 50: return False
    if SCRIPT_RE.search(text) or HTML_TAG_RE.search(text): return False
    return True
