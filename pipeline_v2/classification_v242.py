"""V2.4.2 child-segment rules.

Parent identity intentionally delegates to the V2.4.1 lock. This module only
adds conservative evidence rules for child segments and does not contain
record-specific answers.
"""
from __future__ import annotations

import re
from pathlib import Path

from .classification_v23 import category_v23
from .classification_v24 import category_v24
from .classification_v241 import lock_parent_type_v241, sentence_title_reasons


ACTION_WORDS = r"负责|组织|采取|立即|按照|根据|发生|确认|开展|编制|发布|修订|执行|接到|结合"
PUBLICATION_WORDS = r"已经.{0,40}(?:审议通过|同意|批准)|现予公布|现印发|请认真贯彻执行|自.{0,20}起施行"
FIRST_AID_WORDS = r"需要立即就医|向现场的医生|立即呼叫医生|立即就医|出示此安全技术说明书"
REGULATION_LINE_RE = re.compile(r"^[《]?[^\n。；;]{2,80}(?:规程|规定|办法|条例|标准)[》]?$")
SECTION_HEADING_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)\s*[^\n。；;]{2,80}$"
)
SPECIAL_RE = re.compile(r"(?:专项应急预案|专项预案|现场处置方案|综合应急预案|应急救援预案)")


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value or "").strip("。；;，,|｜")


def _line_candidates(text: str) -> list[str]:
    return [line.strip("《》 |｜") for line in (text or "").splitlines() if line.strip()]


def _is_toc_text(value: str) -> bool:
    value = value or ""
    return bool(
        re.search(r"(?:\.{5,}|…{3,}|-{4,})\s*\d{1,4}\s*$", value)
        or len(re.findall(r"(?:\.{3,}|…{2,})\s*\d{1,4}", value)) >= 2
        or len(re.findall(r"\d+(?:\.\d+){0,3}\s*[^\n]{2,40}(?:专项应急预案|现场处置方案)", value)) >= 2
    )


def _is_section_title(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) > 100 or _is_toc_text(value):
        return False
    if not SECTION_HEADING_RE.match(value):
        return False
    phrase = re.sub(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)\s*", "", value)
    if re.match(rf"^(?:{ACTION_WORDS})", phrase) or phrase.endswith(("。", "；", ";", "，", ",")):
        return False
    return len(re.findall(r"[\u4e00-\u9fff]", phrase)) >= 2


def _is_nominal(value: str, parent_type: str = "") -> bool:
    value = (value or "").strip("《》 ")
    if not value or _is_toc_text(value):
        return False
    if re.search(FIRST_AID_WORDS, value) or re.search(r"(?:部令|第\d+号)", value):
        return False
    if SPECIAL_RE.search(value):
        return not bool(sentence_title_reasons(value, has_heading_evidence=True))
    if parent_type == "regulation" and REGULATION_LINE_RE.fullmatch(value):
        return True
    if parent_type == "sds" and ("安全技术说明书" in value or "安全数据表" in value):
        return True
    return bool(re.search(r"(?:指南|指导意见|指导手册|实施解读|处置原则|事故调查报告|事故通报|事故案例)$", value))


def _case_evidence(text: str, title: str = "") -> dict[str, bool]:
    probe = f"{title}\n{(text or '')[:8000]}"
    return {
        "date_or_time": bool(re.search(r"(?:20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}[·.]\d{1,2}|事故发生于|发生时间)", probe)),
        "enterprise_or_place": bool(re.search(r"(?:有限公司|公司|企业|厂区|矿井|项目部|作业现场|某地|某市|某县)", probe)),
        "harm_or_rescue": bool(re.search(r"(?:死亡|受伤|伤亡|失联|救援|经济损失|中毒|窒息|晕倒)", probe)),
        "cause_or_lesson": bool(re.search(r"(?:事故原因|直接原因|间接原因|责任追究|事故教训|防范措施|调查结论)", probe)),
    }


def is_concrete_accident_case(text: str, title: str = "") -> bool:
    """Require three independent case evidence classes, never a keyword alone."""
    evidence = _case_evidence(text, title)
    explicit_case_marker = bool(re.search(
        r"中毒事故|窒息事故|事故经过|事故原因|事故教训|事故调查|事故通报|事故伤亡",
        f"{title}\n{(text or '')[:1800]}",
    ))
    # Guidance documents often mention companies, rescue and causes as generic
    # management examples. An explicit date plus a case-narrative marker is
    # required before those words can form an accident case.
    return sum(evidence.values()) >= 3 and evidence["date_or_time"] and explicit_case_marker


def _has_complete_actions(text: str) -> bool:
    probe = text or ""
    action_hits = len(re.findall(r"报警|疏散|警戒|灭火|救援|防护|转移|切断|处置|通风|检测", probe[:5000]))
    return action_hits >= 3 and len(re.findall(r"[。；;]", probe[:5000])) >= 2


def is_reference_only(title: str, text: str) -> bool:
    probe = f"{title}\n{text or ''}"
    if _has_complete_actions(text):
        return False
    return len(re.findall(r"(?:执行|参照|按照|启动|见|详见).{0,80}(?:预案|规定|章节)", probe)) >= 1 and len((text or "").strip()) <= 700


def is_toc_fragment(title: str, text: str) -> bool:
    return _is_toc_text(title) or _is_toc_text(text[:1200] if text else "")


def is_onsite_disposal(title: str, text: str) -> bool:
    if re.search(r"现场处置方案|现场处置要点|应急处置要点|处置卡|岗位处置", title or ""):
        return _has_complete_actions(text) or len(text or "") >= 120
    return False


def is_guidance_section(title: str, text: str, parent_type: str) -> bool:
    if parent_type != "guidance_reference" or is_concrete_accident_case(text, title):
        return False
    if not _is_section_title(title):
        return False
    body = (text or "").strip()
    return len(body) >= 40 and len(re.findall(r"[。；;]", body)) >= 1 and not is_reference_only(title, text)


def recover_title_v242(
    original_title: str,
    current_title: str,
    text: str,
    parent_text: str,
    source_file: str,
    current_segment_type: str,
    parent_type: str,
) -> dict[str, object]:
    """Recover only titles evidenced by source text; never synthesize a topic."""
    original = (original_title or "").strip()
    current = (current_title or "").strip()
    body = text or ""
    parent = parent_text or ""

    # TOC records retain the source item and are explicitly not valid headings.
    toc_match = re.search(r"(?m)^\s*(\d+(?:\.\d+)*\s*[^\n]{2,100}(?:专项应急预案|现场处置方案))\s*(?:\.{3,}|…{2,}|-{3,})\s*\d+", original)
    if toc_match or _is_toc_text(original) or _is_toc_text(body[:900]):
        candidate = toc_match.group(1).strip() if toc_match else re.sub(r"\s*(?:\.{3,}|…{2,}|-{3,}).*$", "", original).strip()
        return {"title": candidate, "status": "toc_only", "source": "toc_entry", "evidence": ["目录条目带页码或点线"], "warnings": ["toc_only"]}

    # V2.4.1 may already have recovered a complete case heading while the
    # source record title is a truncated web-label prefix.
    if (
        current
        and is_concrete_accident_case(body, current)
        and len(current) <= 100
        and not sentence_title_reasons(current, has_heading_evidence=True)
        and re.search(r"事故", current)
    ):
        return {"title": current, "status": "valid", "source": "body_first_heading", "evidence": ["正文开头存在完整事故标题"], "warnings": []}

    # A source record may contain an article title after a page/article prefix.
    pipe_parts = [part.strip() for part in re.split(r"[｜|]", original) if part.strip()]
    for candidate in reversed(pipe_parts):
        if re.search(PUBLICATION_WORDS, candidate) or re.search(r"[。；;]", candidate):
            continue
        if _is_section_title(candidate) or (_is_nominal(candidate, parent_type) and not sentence_title_reasons(candidate, has_heading_evidence=True)):
            return {"title": candidate, "status": "valid", "source": "source_record_heading", "evidence": ["原始记录中存在独立章节标题"], "warnings": []}

    # The actual title may be quoted in a government notice. Select the title
    # inside the book marks, not the notification paragraph around it.
    quoted = re.findall(r"《([^》]{4,140})》", f"{original}\n{parent[:12000]}\n{body[:1200]}")
    quoted = [re.sub(r"\s+", "", item).strip("。；;，,") for item in quoted]
    for candidate in quoted:
        if SPECIAL_RE.search(candidate) or (parent_type == "regulation" and REGULATION_LINE_RE.fullmatch(candidate)):
            return {"title": candidate, "status": "valid", "source": "attachment_title", "evidence": ["书名号中的正式标题"], "warnings": []}

    # Recover a clean numbered heading from a glued title/body slice.
    for candidate in [original, current] + _line_candidates(body[:1000]):
        candidate = candidate.strip()
        match = re.match(r"^(\d+(?:\.\d+){0,4}\s*[^\n。；;]{2,80}?)(?=(?:一旦|作业现场|组织相关|按照|根据|立即|发生|负责)|$)", candidate)
        if match and _is_section_title(match.group(1).strip()):
            return {"title": match.group(1).strip(), "status": "valid", "source": "body_heading", "evidence": ["编号章节标题与正文粘连后已按原文截断"], "warnings": ["body_title_concatenation_fixed"]}

    # SDS identity is printed in the parsed front. Combining the explicitly
    # printed product name with the printed SDS phrase is traceable, not a guess.
    if parent_type == "sds":
        product = re.search(r"(?:产品说明|产品名称|化学品名称|化學品名稱)\s*[:：]?\s*([^\n;；]{1,60})", parent[:9000], re.I)
        if product:
            chemical = product.group(1).strip(" 。;；")
            if chemical and not re.search(r"安全技术说明书|安全数据表", chemical):
                return {"title": f"{chemical} 化学品安全技术说明书", "status": "valid", "source": "sds_identity_heading", "evidence": ["产品名称与SDS章节标题均在来源正文"], "warnings": ["first_aid_title_recovered"]}
        if re.search(FIRST_AID_WORDS, original):
            return {"title": "化学品安全技术说明书", "status": "uncertain", "source": "sds_section_fallback", "evidence": ["原始标题为急救正文，未能确定化学品名称"], "warnings": ["first_aid_text_as_title"]}

    # Regulations should use the first formal name, excluding department-order
    # publication notes and dates.
    if parent_type == "regulation":
        for line in _line_candidates(parent[:12000]):
            candidate = line.strip("《》 ")
            if REGULATION_LINE_RE.fullmatch(candidate) and not re.search(PUBLICATION_WORDS, candidate):
                return {"title": candidate, "status": "valid", "source": "document_title", "evidence": ["正文中的正式法规名称"], "warnings": []}

    # Preserve a clean onsite heading even when the current slice starts in its
    # first body sentence.
    for candidate in [original, current]:
        if _is_section_title(candidate) and not sentence_title_reasons(candidate, has_heading_evidence=True):
            return {"title": candidate, "status": "valid", "source": "source_record_heading", "evidence": ["原始记录中的完整编号标题"], "warnings": []}

    # A short, reliable nominal source title is acceptable.
    for candidate in [original, current, Path(source_file).stem]:
        candidate = candidate.strip()
        if _is_nominal(candidate, parent_type) and not sentence_title_reasons(candidate, has_heading_evidence=True):
            return {"title": candidate, "status": "valid", "source": "source_title", "evidence": ["原始来源中的完整名词性标题"], "warnings": []}

    if original and (sentence_title_reasons(original, has_heading_evidence=False) or re.search(PUBLICATION_WORDS, original)):
        return {"title": original, "status": "invalid", "source": "source_record", "evidence": ["原始标题疑似正文句、残句或发布说明"], "warnings": ["invalid_title_candidate"]}
    if original:
        return {"title": original, "status": "uncertain", "source": "source_record", "evidence": ["没有可靠的版面标题证据"], "warnings": ["title_recovery_uncertain"]}
    return {"title": "", "status": "missing", "source": "missing", "evidence": [], "warnings": ["title_missing"]}


def segment_type_v242(
    title: str,
    text: str,
    parent_type: str,
    sibling_count: int,
    title_status: str,
    original_title: str = "",
    base_segment_type: str = "full_document",
) -> str:
    if parent_type == "sds":
        return "full_document"
    if re.search(r"SDS附件|安全技术说明书|安全数据表|物質安全資料表", title or "", re.I) and parent_type != "sds":
        return "appendix_sds"
    if parent_type == "chemical_catalog":
        return "table"
    if is_toc_fragment(title, text):
        return "toc_fragment"
    if is_reference_only(title, text):
        return "reference_sentence"
    if is_onsite_disposal(title, text):
        return "onsite_disposal_plan"
    if parent_type == "guidance_reference" and is_concrete_accident_case(text, title):
        return "accident_case_section"
    if is_guidance_section(title, text, parent_type):
        return "guidance_section"
    if re.search(r"(?:职责|编制依据|风险原因|处置步骤)", title or "") and len(text or "") < 700:
        return "list_fragment"
    if re.match(r"^[（(]?\d+[）)、.．]", title or "") and len(text or "") < 700 and not SPECIAL_RE.search(title or ""):
        return "list_fragment"
    core = sum(bool(re.search(pattern, (text or "")[:9000])) for pattern in (r"适用范围", r"事故风险|危险性分析", r"组织机构|应急组织", r"应急响应|响应启动", r"应急处置|处置措施", r"应急保障"))
    if title_status == "valid" and SPECIAL_RE.search(title or "") and core >= 2:
        return "embedded_special_section"
    if parent_type == "guidance_reference" and base_segment_type == "accident_case_section":
        return "full_document"
    # Preserve a stable V2.4.1 type when the new evidence rules found no
    # contradiction. This avoids changing unrelated records in a point fix.
    allowed = {
        "full_document", "embedded_special_section", "onsite_disposal_plan", "guidance_section",
        "accident_case_section", "reference_sentence", "list_fragment", "toc_fragment", "table",
        "appendix_sds", "embedded_sds",
    }
    return base_segment_type if base_segment_type in allowed else "full_document"


def accident_category_v242(title: str, text: str, segment_type: str, base_category: str = "other") -> str:
    if segment_type == "accident_case_section" and re.search(r"中毒|窒息|一氧化碳|硫化氢|缺氧", f"{title}\n{text[:2000]}"):
        return "poisoning_asphyxia"
    if re.search(r"指导意见|指导手册|实施解读|应急处置原则", title or "") and re.search(r"泄漏", title or ""):
        return "chemical_leakage"
    return category_v24(title, text) if title or text else category_v23(title, text)


__all__ = [
    "lock_parent_type_v241", "recover_title_v242", "segment_type_v242", "accident_category_v242",
    "is_concrete_accident_case", "is_toc_fragment", "is_reference_only", "is_onsite_disposal",
]
