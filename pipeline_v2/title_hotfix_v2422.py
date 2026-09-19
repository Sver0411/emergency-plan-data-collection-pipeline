"""V2.4.2.2 full-document title recovery and validation hotfix.

This module runs only after the final segment type has been frozen.  It does
not classify parent documents, child segments, accident categories, or roles.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .title_audit_v242 import audit_title_consistency_v242


FULL_DOCUMENT = "full_document"
SDS_TITLE_CN = "化学品安全技术说明书"

TITLE_SUFFIX_RE = re.compile(
    r"(?:应急预案|应急救援预案|规程|规定|办法|条例|指南|指导意见|指导手册|"
    r"实施解读|应急处置原则|安全措施和应急处置原则|事故调查报告|事故通报)$"
)
PLAN_TITLE_RE = re.compile(r"(?:应急预案|应急救援预案)$")
REGULATION_TITLE_RE = re.compile(r"(?:规程|规定|办法|条例)$")
GUIDANCE_TITLE_RE = re.compile(r"(?:指南|指导意见|指导手册|实施解读|应急处置原则|正式实施)$")
CASE_TITLE_RE = re.compile(r"事故.*(?:通报|报告)$|通报.*事故|事故调查报告")
FIRST_LINE_SPECIAL_PLAN_RE = re.compile(
    r"^(?:[一二三四五六七八九十百]+\s+|\d+(?:\.\d+)*\s+)?"
    r"[^。；;：:\n]{2,70}(?:专项应急预案|事故应急预案|现场处置方案|应急处置方案)$"
)

FIRST_AID_RE = re.compile(
    r"给患者喝下|需要立即就医|向现场的医生|立即就医|呼叫医生|漱口|不要催吐|"
    r"冲洗(?:至少|皮肤|眼睛)|人工呼吸|心肺复苏"
)
SUPPLIER_RE = re.compile(r"供应商详细信息|供应商资料|厂商资料|廠商資料|企业名称\s*[:：]|緊急聯絡電話")
SDS_SECTION_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十0-9]+(?:部分|章|节)?\s*)?"
    r"(?:化学品与厂商资料|化學品與廠商資料|化学品及企业标识|标识|"
    r"危险性概述|危害辨识资料|急救措施|消防措施|泄漏应急处理)$"
)
GENERIC_SECTION_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十百]+[、.．]|"
    r"\d+(?:\.\d+){0,4}[、.．]?)\s*"
    r"(?:应急响应|适用范围|一般规定|总则|编制目的|编制依据|组织机构|组织体系|"
    r"事故风险分析|风险分析|应急处置|处置措施|应急保障|附则|矿山救援一般规定)$"
)
NUMBERED_SECTION_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十百]+(?:[、.．]|\s+)|"
    r"\d+(?:\.\d+){0,4}[、.．]?)\s*.+"
)
LIST_OR_REFERENCE_RE = re.compile(r"^[（(]?\d{1,2}[）)、.．]\s*[《]|^[（(]?\d{1,2}[）)、.．]\s*(?:负责|组织|按照|根据)")
PUBLICATION_RE = re.compile(r"已经.{0,40}(?:审议通过|同意|批准)|现予公布|现印发|请认真贯彻执行|自.{0,20}起施行")
BODY_SENTENCE_RE = re.compile(
    r"^(?:负责|组织|采取|立即|按照|根据|发生|确认|开展|编制|制定|发布|修订|执行|需要立即|"
    r"给患者|速按照|8起事故分别是)"
)
TOC_RE = re.compile(r"(?:\.{5,}|…{3,}|-{5,})\s*\d{1,4}\s*$")

PREFLIGHT_STRUCTURE_PATTERNS = {
    "risk": r"事故风险|风险分析|危险性分析|危害程度分析|危险有害因素|事故类型",
    "organization": r"组织机构|组织体系|应急组织|应急指挥部|指挥部.{0,30}(?:负责|组织|领导)",
    "response": r"应急响应|响应分级|响应程序|预警启动|信息报告|报警|启动.{0,20}预案|扩大应急",
    "disposal": r"应急处置|处置措施|现场处置|应急救援|疏散|警戒|抢险",
    "support": r"应急保障|保障措施|救援器材|救援物资|救援装备",
}

TOPIC_PATTERNS = {
    "natural_disaster": r"自然灾害|地震|雷击|暴雨|洪水|暴雪|台风|大风|高温|严寒",
    "collapse": r"建设工程|建筑施工|深基坑|模板|脚手架|塔吊|坍塌",
    "chemical_leakage": r"危险化学品|危化品|化学品泄漏|泄漏事故|堵漏",
    "poisoning_asphyxia": r"中毒|窒息|缺氧|硫化氢|一氧化碳",
    "confined_space": r"有限空间|受限空间",
    "fire_explosion": r"火灾|爆炸|燃烧|易燃",
}


def _compact_spaces(value: str) -> str:
    return re.sub(r"[\t\u3000 ]+", " ", value or "").strip()


def _front_lines(text: str, limit: int = 60) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = _compact_spaces(raw).strip("|｜")
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def first_line_special_plan_evidence(record: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a real special-plan title printed as the first local line.

    Chinese sequence markers such as “五” or “六” are allowed only when the
    local body independently proves a complete, topic-continuous plan.  This
    keeps ordinary numbered chapters and TOC entries excluded.
    """
    text = str(record.get("raw_text") or record.get("segment_text") or "")
    lines = _front_lines(text)
    if not lines:
        return None
    first = _clean_candidate(lines[0])
    if not FIRST_LINE_SPECIAL_PLAN_RE.fullmatch(first):
        return None
    if TOC_RE.search(lines[0]) or re.search(r"(?:\.{3,}|…{2,}|-{4,})\s*\d+", lines[0]):
        return None
    if GENERIC_SECTION_RE.fullmatch(first) or BODY_SENTENCE_RE.search(first) or PUBLICATION_RE.search(first):
        return None
    if FIRST_AID_RE.search(first) or SUPPLIER_RE.search(first) or first.endswith(("：", ":", "；", ";", "。")):
        return None

    body = "\n".join(lines[1:]) + "\n" + "\n".join((text or "").splitlines()[len(lines[:1]):])
    structures = {
        name for name, pattern in PREFLIGHT_STRUCTURE_PATTERNS.items()
        if re.search(pattern, body[:12000])
    }
    if len(structures) < 2:
        return None

    category = str(record.get("accident_category") or "")
    title_topics = {name for name, pattern in TOPIC_PATTERNS.items() if re.search(pattern, first)}
    body_topics = {name for name, pattern in TOPIC_PATTERNS.items() if re.search(pattern, body[:5000])}
    expected_topics = set(title_topics)
    if category in TOPIC_PATTERNS:
        expected_topics.add(category)
    if expected_topics and not (expected_topics & body_topics):
        return None
    if not title_topics and not any(token in body[:5000] for token in re.findall(r"[\u4e00-\u9fff]{2,8}", first)):
        return None
    return {
        "title": first,
        "structures": sorted(structures),
        "title_topics": sorted(title_topics),
        "body_topics": sorted(body_topics),
        "category": category,
    }


def _clean_candidate(value: str) -> str:
    value = _compact_spaces(value).strip("《》〈〉|｜")
    value = re.sub(r"^[附件附录]+\s*[:：]?\s*", "", value)
    value = re.sub(r"[（(](?:草案|征求意见稿|修订稿|试行)[）)]$", "", value).strip()
    value = re.sub(r"\s+(?:20\d{2}[-年].*)$", "", value).strip()
    return value.strip("《》〈〉|｜")


def full_document_title_issues(title: str, parent_type: str = "", source: str = "") -> list[str]:
    """Return hard validation failures for a full-document title."""
    value = _compact_spaces(title)
    issues: list[str] = []
    if not value:
        return ["missing_title"]
    if len(value) > 80:
        issues.append("over_80_chars")
    if TOC_RE.search(value) or len(re.findall(r"(?:\.{3,}|…{2,})\s*\d+", value)) >= 1:
        issues.append("toc_entry_or_dots")
    if FIRST_AID_RE.search(value):
        issues.append("first_aid_sentence")
    if SUPPLIER_RE.search(value):
        issues.append("supplier_information")
    if SDS_SECTION_RE.fullmatch(value):
        issues.append("sds_section_heading")
    if GENERIC_SECTION_RE.fullmatch(value):
        issues.append("body_secondary_heading")
    if NUMBERED_SECTION_RE.match(value) and re.search(r"专项应急预案|现场处置方案", value):
        issues.append("numbered_body_section")
    if LIST_OR_REFERENCE_RE.search(value):
        issues.append("list_or_legal_reference")
    if PUBLICATION_RE.search(value):
        issues.append("publication_statement")
    if BODY_SENTENCE_RE.search(value):
        issues.append("body_sentence_or_fragment")
    if value.endswith(("：", ":", "；", ";")):
        issues.append("sentence_punctuation_ending")
    if re.match(r"^(?:第一节\s*一般规定|第五章\s*矿山救援一般规定)$", value):
        issues.append("forbidden_generic_chapter")
    if parent_type == "sds" and not re.search(r"安全技术说明书|安全资料表|SDS", value, re.I):
        issues.append("sds_title_missing_document_marker")
    if source == "toc_entry":
        issues.append("toc_title_source")
    return list(dict.fromkeys(issues))


def _candidate_matches_parent(candidate: str, parent_type: str) -> bool:
    value = _clean_candidate(candidate)
    if full_document_title_issues(value, parent_type):
        return False
    if parent_type in {
        "government_or_regional_plan", "enterprise_plan", "enterprise_group_plan",
        "organization_plan", "manual_review",
    }:
        return bool(PLAN_TITLE_RE.search(value))
    if parent_type == "regulation":
        return bool(REGULATION_TITLE_RE.search(value))
    if parent_type == "guidance_reference":
        return bool(GUIDANCE_TITLE_RE.search(value) or re.search(r"PubChem安全数据$", value))
    if parent_type == "accident_case":
        return bool(CASE_TITLE_RE.search(value))
    return bool(TITLE_SUFFIX_RE.search(value))


def _book_title_candidates(lines: list[str], parent_type: str) -> list[str]:
    output: list[str] = []
    for line in lines[:16]:
        if re.match(r"^[（(]?\d+[）)]", line):
            continue
        # “制定《……》” is commonly a web article label rather than the
        # attachment's canonical title.  Prefer the standalone quoted title
        # printed on the following line when it exists.
        if re.match(r"^制定《", line):
            continue
        for quoted in re.findall(r"《([^》]{3,100})》", line):
            candidate = _clean_candidate(quoted)
            if _candidate_matches_parent(candidate, parent_type):
                output.append(candidate)
    return output


def _line_title_candidates(lines: list[str], parent_type: str) -> list[str]:
    output: list[str] = []
    for index, line in enumerate(lines[:20]):
        if re.search(r"正文内容区域|请使用tab|^[-—]?\s*[IVXⅠⅡⅢⅣⅤ]+\s*[-—]?$", line, re.I):
            continue
        candidate = _clean_candidate(line)
        if _candidate_matches_parent(candidate, parent_type):
            output.append(candidate)
        if index + 1 < min(len(lines), 20):
            joined = _clean_candidate(f"{line}{lines[index + 1]}")
            if len(joined) <= 80 and _candidate_matches_parent(joined, parent_type):
                output.append(joined)
    return output


def _clean_source_file_title(source_file: str) -> str:
    stem = Path(source_file or "").stem
    stem = re.sub(r"-[0-9a-f]{10,}$", "", stem, flags=re.I)
    stem = re.sub(r"^(?:PDF\s+)", "", stem, flags=re.I)
    stem = re.sub(r"\s+-\s+[^-]{2,80}$", "", stem)
    return _clean_candidate(stem)


def _clean_chemical_name(value: str) -> str:
    value = _compact_spaces(value).strip("·:：；;。")
    value = re.split(
        r"\s*(?:按照\s*GB|修订日期|編製日期|製表日期|SDS\s*编号|版本(?:号)?\s*[:：]|"
        r"中文别名|中文別名|英文名称|英文名稱)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = re.sub(r"\s*[（(]\s*\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?\s*mL\s*[）)]", "", value, flags=re.I)
    if re.search(r"[\u4e00-\u9fff]", value):
        value = re.sub(r"\s*\(([A-Za-z][^)]{1,60})\)\s*$", "", value)
    value = value.strip(" ,，")
    if len(value) > 70 or re.search(r"企业|供應商|供应商|电话|地址|安全技术说明书|安全資料表", value):
        return ""
    return value


def extract_sds_identity(record: dict[str, Any]) -> tuple[str, str]:
    """Extract a printed product/chemical identity without inferring chemistry."""
    text = (record.get("raw_text") or record.get("segment_text") or "")[:12000]
    patterns = [
        r"(?:化学品名称|化學品名稱)\s*[:：]\s*([^\n;；]{1,80})",
        r"(?:化学品中文名|化學品中文名|中文名称|中文名稱)\s*[:：]\s*([^\n;；]{1,80})",
        r"(?:产品中文名称|產品中文名稱)\s*[:：]?\s*([^\n;；]{1,80})",
        r"(?:化学品中文\s*[（(]英文\s*[）)]名称\s*,?\s*化学品俗名或商品名|"
        r"化學品中文\s*[（(]英文\s*[）)]名稱\s*,?\s*化學品俗名或商品名)\s*[:：]\s*([^\n;；]{1,80})",
        r"(?:产品名称|產品名稱|产品说明|產品說明)\s*[:：]\s*([^\n;；]{1,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            identity = _clean_chemical_name(match.group(1))
            if identity:
                return identity, "sds_identity_section"

    sds_data = record.get("sds") or {}
    identity = _clean_chemical_name(str(sds_data.get("chemical_name_cn") or ""))
    if identity:
        return identity, "structured_sds_identity"

    lines = _front_lines(text, 12)
    for index, line in enumerate(lines):
        if re.search(r"安全技术说明书|安全資料表|物質安全資料表", line, re.I) and index + 1 < len(lines):
            identity = _clean_chemical_name(lines[index + 1])
            if identity and not re.match(r"版本|报告编号|編製|修订|第\s*\d", identity):
                return identity, "sds_cover_product_name"
    return "", "sds_identity_missing"


def _format_sds_title(identity: str, text: str) -> str:
    traditional_data_sheet = bool(re.search(r"安全資料表|物質安全資料表|化學品與廠商資料", text[:2500], re.I))
    if traditional_data_sheet:
        separator = "" if re.search(r"[（(][^）)]{1,20}[）)]$", identity) else " "
        return f"{identity}{separator}安全资料表（SDS）"
    return f"{identity} {SDS_TITLE_CN}"


def recover_full_document_title_v2422(record: dict[str, Any]) -> dict[str, Any]:
    """Recover a full-document title after segment classification is final."""
    if record.get("segment_type") != FULL_DOCUMENT:
        raise ValueError("V2.4.2.2 title recovery accepts full_document records only")

    parent_type = str(record.get("parent_document_type") or "")
    raw_text = str(record.get("raw_text") or record.get("segment_text") or "")
    current = _compact_spaces(str(record.get("title") or ""))
    original = _compact_spaces(str(record.get("original_title") or ""))
    current_source = str(record.get("title_source") or "")

    if parent_type == "sds":
        identity, identity_source = extract_sds_identity(record)
        if identity:
            title = _format_sds_title(identity, raw_text)
            return {
                "title": title,
                "status": "valid",
                "source": identity_source,
                "evidence": ["最终segment_type为full_document后，从SDS化学品/产品身份字段恢复整份文档标题"],
                "reasons": ["sds_identity_title_recovery"],
            }
        return {
            "title": SDS_TITLE_CN,
            "status": "uncertain",
            "source": "sds_identity_missing",
            "evidence": ["来源正文可确认SDS，但未能可靠提取化学品或产品名称"],
            "reasons": ["sds_generic_title_manual_review"],
        }

    canonical_fields = (
        "parent_canonical_title", "canonical_parent_title", "parent_document_title", "document_title",
    )
    for field in canonical_fields:
        candidate = _clean_candidate(str(record.get(field) or ""))
        if candidate and _candidate_matches_parent(candidate, parent_type):
            return {
                "title": candidate, "status": "valid", "source": "parent_canonical_title",
                "evidence": [f"父文档规范标题字段：{field}"], "reasons": ["parent_canonical_title"],
            }

    trusted_current_sources = {
        "document_title", "attachment_title", "source_record_heading", "raw_text_document_title",
        "existing_verified_title", "source_title",
    }
    if current_source in trusted_current_sources and _candidate_matches_parent(current, parent_type):
        return {
            "title": current, "status": "valid", "source": "parent_standard_title",
            "evidence": [f"冻结结果中的父文档规范标题，原来源={current_source}"],
            "reasons": ["parent_standard_title_retained"],
        }

    lines = _front_lines(raw_text)
    # A standalone cover/H1 line outranks a book title quoted inside an
    # introductory or legal-basis sentence.
    front_candidates = _line_title_candidates(lines, parent_type) + _book_title_candidates(lines, parent_type)
    if front_candidates:
        candidate = front_candidates[0]
        source = "cover_or_first_page_title"
        if parent_type == "accident_case":
            source = "article_title"
        return {
            "title": candidate, "status": "valid", "source": source,
            "evidence": ["当前记录正文前部的正式标题，且通过full_document硬性校验"],
            "reasons": ["post_segment_type_title_recovery"],
        }

    # The original source title is lower priority than a cover/H1 but can still
    # be used when it is itself a complete nominal document title.
    original_candidates = _book_title_candidates([original], parent_type) + [_clean_candidate(original)]
    for candidate in original_candidates:
        if _candidate_matches_parent(candidate, parent_type):
            return {
                "title": candidate, "status": "valid", "source": "original_source_title",
                "evidence": ["原始来源记录中的完整文档标题"], "reasons": ["original_source_title"],
            }

    file_title = _clean_source_file_title(str(record.get("source_file") or ""))
    file_candidates = _book_title_candidates([file_title], parent_type) + [file_title]
    for candidate in file_candidates:
        if _candidate_matches_parent(candidate, parent_type):
            return {
                "title": candidate, "status": "uncertain", "source": "cleaned_filename",
                "evidence": ["仅能从清理后的文件名恢复，等待人工核验"], "reasons": ["filename_fallback"],
            }

    # Preserve a traceable topic label as uncertain when it is not itself a
    # forbidden body/TOC fragment.  Do not promote it to valid.
    current_issues = full_document_title_issues(current, parent_type, current_source)
    if current and not current_issues and current_source == "local_topic_fallback":
        return {
            "title": current, "status": "uncertain", "source": current_source,
            "evidence": ["现有主题标题缺少封面/H1证据"], "reasons": ["topic_only_manual_review"],
        }

    first_line_evidence = first_line_special_plan_evidence(record)
    if first_line_evidence:
        return {
            "title": first_line_evidence["title"],
            "status": "valid",
            "source": "raw_text_document_title",
            "evidence": [
                "raw_text首个有效非空行是独立专项预案标题",
                f"正文结构证据={first_line_evidence['structures']}",
                f"标题/正文主题证据={first_line_evidence['title_topics']}/{first_line_evidence['body_topics']}",
            ],
            "reasons": ["preflight_first_line_special_plan_title"],
            "first_line_special_plan_verified": True,
        }

    return {
        "title": "", "status": "missing", "source": "missing",
        "evidence": ["未找到可追溯的父文档、封面、H1、首页、原始记录或文件名标题"],
        "reasons": ["no_reliable_full_document_title"],
    }


def audit_full_document_title_v2422(record: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Run the hard checks and the V2.4.2 title/body consistency audit."""
    title = str(decision.get("title") or "")
    status = str(decision.get("status") or "missing")
    source = str(decision.get("source") or "missing")
    issues = full_document_title_issues(title, str(record.get("parent_document_type") or ""), source)
    if decision.get("first_line_special_plan_verified"):
        issues = [issue for issue in issues if issue != "numbered_body_section"]
    if status == "valid" and issues:
        status = "invalid"
    if status == "toc_only":
        status = "uncertain" if title else "missing"
        issues.append("full_document_toc_only_conflict")

    consistency = audit_title_consistency_v242(
        document_id=str(record.get("document_id") or ""),
        title=title,
        text=str(record.get("raw_text") or ""),
        segment_type=FULL_DOCUMENT,
        title_status=status,
        title_source=source,
        sections=record.get("sections") or [],
    )
    if issues:
        consistency = dict(consistency)
        consistency["status"] = "inconsistent" if status == "invalid" else "uncertain"
        consistency["reasons"] = list(dict.fromkeys(list(consistency.get("reasons") or []) + issues))
        consistency["v2_4_2_2_hard_issues"] = issues
    else:
        consistency = dict(consistency)
        consistency["v2_4_2_2_hard_issues"] = []
    return {"status": status, "issues": issues, "consistency": consistency}


__all__ = [
    "extract_sds_identity", "full_document_title_issues", "recover_full_document_title_v2422",
    "audit_full_document_title_v2422", "first_line_special_plan_evidence",
]
