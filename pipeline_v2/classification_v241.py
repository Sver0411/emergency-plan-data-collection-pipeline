from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .classification import _evidence, _result
from .classification_v23 import category_v23
from .classification_v24 import ParentLock, _embedded_types, category_v24, lock_parent_type


GUIDANCE_RE = re.compile(r"指南|指导意见|指导手册|实施解读|应急处置原则|新要求解读")
PLAN_TITLE_RE = re.compile(r"专项应急预案|综合应急预案|事故应急预案|现场处置方案")
NOMINAL_TITLE_RE = re.compile(
    r"(?:综合应急预案|专项应急预案|事故应急预案|现场处置方案|指导手册|指导意见|指南|实施解读|应急处置原则|事故(?:调查报告|案例|通报))$"
)
ACTION_START_RE = re.compile(r"^(?:负责|组织|采取|立即|按照|根据|发生|确认|开展|编制|发布|修订|执行|接到|结合)")
INVALID_TITLE_RE = re.compile(
    r"速按照本综合应急预案|采取桌面演练的形式|组织相关人员核实事故情况|负责制定和实施|48小时以上|24小时以上"
)


@dataclass
class TitleDecision:
    title: str
    status: str
    source: str
    evidence: list[str]
    warnings: list[str]


def lock_parent_type_v241(document_id: str, source_title: str, parent_text: str, original_type: str) -> ParentLock:
    """Small V2.4.1 parent overlay; parent identity is still locked before segments."""
    text = parent_text or ""
    front_len = max(3000, min(len(text), int(len(text) * 0.2) or len(text)))
    front = text[:front_len]
    front_title_lines = "\n".join(line.strip() for line in front.splitlines() if line.strip())[:800]
    title_probe = f"{source_title}\n{front_title_lines}"

    # A selected row that is explicitly a lower-level disposal fragment cannot
    # borrow a cover from a larger parser artifact to become a confident parent.
    partial = _evidence(source_title, "partial_segment_title", r"^\s*\d+(?:\.\d+)*[.、．]?\s*.{0,40}(?:现场处置要点|处置措施|处置程序)", "weak")
    if partial and not PLAN_TITLE_RE.search(source_title):
        result = _result(
            document_id,
            "manual_review",
            "full_document",
            category_v24(source_title, text[:1500]),
            "manual_review",
            ["选中内容为缺少封面和开头的局部处置片段"],
            [partial],
            force_review=True,
            confidence_cap=0.5,
        )
        return ParentLock(result, "dominant_structure", [], source_title.strip())

    # Guidance identity comes from its own title/filename and therefore wins
    # over quoted regulations or a First/Second Article writing style.
    guidance = _evidence(title_probe, "guidance_title", GUIDANCE_RE.pattern)
    if guidance:
        guide_identity = _evidence(front, "guidance_self_reference", r"本指南|本指导意见|本手册|本解读")
        guide_scope = _evidence(front, "guidance_purpose", r"应急准备|指导企业|工作指南|操作指导|管理培训")
        role = "emergency_preparedness_guidance" if re.search(r"应急准备指南", title_probe) else "guidance_reference"
        result = _result(
            document_id,
            "guidance_reference",
            "full_document",
            category_v24(source_title, front),
            role,
            ["文件自身标题明确为指南、指导意见、手册、解读或处置原则"],
            [item for item in (guidance, guide_identity, guide_scope) if item],
            force_review=False,
        )
        return ParentLock(result, "title", _embedded_types(text, "guidance_reference"), source_title.strip())

    enterprise_cover = bool(re.search(
        r"(?:有限公司|股份有限公司|集团公司|煤矿).{0,100}(?:生产安全事故|应急预案)|"
        r"(?:编制单位|批准页).{0,100}(?:有限公司|股份有限公司|集团公司|煤矿)",
        front[:5000],
        re.S,
    ))
    regional_title = _evidence(
        title_probe,
        "regional_plan_title",
        r"[\u4e00-\u9fff]{2,16}(?:市|区|县|镇|街道|开发区|园区).{0,35}(?:应急预案|专项预案)|"
        r"(?:本镇|本街道|本开发区|本园区).{0,35}(?:应急预案|专项预案)",
    )
    government_issuer = _evidence(
        front[:7000],
        "government_issuer",
        r"人民政府|管委会|安委会|街道办事处|乡镇政府|镇政府|镇应急救援指挥部|应急管理局|(?:市|区|县|镇).{0,20}应急指挥部",
    )
    territorial_scope = _evidence(
        front[:9000],
        "territorial_scope",
        r"本市行政区域|本区行政区域|本县行政区域|本镇范围|本街道辖区|本园区|本开发区|辖区企业|所在辖区",
    )
    regional_structure = _evidence(
        front[:9000],
        "regional_structure",
        r"成员单位职责|区域资源协调|镇应急联动中心|各部门.{0,20}职责|现场指挥部责任部门",
    )
    government_evidence = [item for item in (regional_title, government_issuer, territorial_scope, regional_structure) if item]
    if not enterprise_cover and government_issuer and (regional_title or territorial_scope or regional_structure) and len(government_evidence) >= 2:
        result = _result(
            document_id,
            "government_or_regional_plan",
            "full_document",
            category_v24(source_title, front),
            "plan_structure_reference",
            ["行政区域标题/发布主体、辖区范围和区域协调结构共同锁定政府或区域预案"],
            government_evidence,
            force_review=False,
        )
        return ParentLock(result, "issuer", _embedded_types(text, "government_or_regional_plan"), source_title.strip())

    return lock_parent_type(document_id, source_title, parent_text, original_type)


def sentence_title_reasons(title: str, *, has_heading_evidence: bool) -> list[str]:
    """A sentence warning needs at least two independent action-sentence signals."""
    title = (title or "").strip()
    if not title or _is_nominal_title(title):
        return []
    if INVALID_TITLE_RE.search(title):
        return ["known_residual_fragment", "action_description"]
    phrase = re.sub(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|[（(]?\d+(?:\.\d+){0,4}[）)、.．]?)\s*", "", title)
    reasons: list[str] = []
    if ACTION_START_RE.match(phrase):
        reasons.append("action_verb_start")
    if title.endswith(("。", "；", ";")):
        reasons.append("sentence_ending")
    if re.search(r"(?:负责|组织|采取|立即|按照|根据|发生|确认|开展|编制|执行).{3,}(?:工作|人员|预案|事故|措施|救援|处置)", phrase):
        reasons.append("concrete_action")
    if re.search(r"(?:公司|部门|人员|指挥部|负责人).{0,12}(?:负责|组织|实施|开展|报告|确认)", phrase):
        reasons.append("subject_predicate")
    if not has_heading_evidence:
        reasons.append("no_heading_evidence")
    return list(dict.fromkeys(reasons)) if len(set(reasons)) >= 2 else []


def _clean_candidate(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value).strip("。；;，,|｜")


def _is_nominal_title(value: str) -> bool:
    value = (value or "").strip()
    if not value or re.match(r"^[（(]?\d+[）)、.．]\s*《", value):
        return False
    if value.count("《") != value.count("》"):
        return False
    return bool(NOMINAL_TITLE_RE.search(value.strip("《》")))


def _first_case_heading(text: str) -> str:
    lines = [line.strip(" |") for line in text[:800].splitlines() if line.strip(" |")]
    for index, line in enumerate(lines[:4]):
        joined = line
        if index + 1 < len(lines) and ("事故" not in joined or joined.endswith(("一", "化工", "行业"))):
            joined += lines[index + 1]
        if re.search(r"(?:公司|企业).{0,30}(?:\d{1,2}[·.月]\d{1,2}|事故)|\d{1,2}[·.]\d{1,2}.{0,25}事故", joined):
            return _clean_candidate(joined)
    return ""


def recover_title_v241(
    original_title: str,
    current_title: str,
    text: str,
    parent_text: str,
    source_file: str,
    current_segment_type: str,
) -> TitleDecision:
    original = (original_title or "").strip()
    current = (current_title or "").strip()

    # A V2.4 title that is both present at the current segment start and
    # traceable to the parent body is real heading evidence.  Preserve it even
    # when the old source slice began later inside a duty list.
    parent_compact = re.sub(r"[\s\u3000]+", "", parent_text)
    current_compact = _clean_candidate(current)
    if (
        current
        and _is_nominal_title(current)
        and (text or "").lstrip().startswith(current)
        and current_compact in parent_compact
    ):
        return TitleDecision(current, "valid", "body_heading", ["当前segment从父文档中的完整标题开始"], [])

    # Duty/reference/list records retain their source text.  Converting them to
    # a plausible nearby plan title would change the record's meaning.
    original_sentence = sentence_title_reasons(original, has_heading_evidence=False)
    list_action = bool(re.match(r"^[（(]?\d+[）)、.]", original)) and bool(original_sentence)
    if current_segment_type in {"list_fragment", "reference_sentence"} or list_action:
        warnings = ["sentence_used_as_title"] if original_sentence else []
        return TitleDecision(original, "uncertain", "source_record", original_sentence, warnings)

    case_heading = _first_case_heading(text)
    if case_heading:
        return TitleDecision(case_heading, "valid", "body_first_heading", ["事故企业、日期和事故标题位于正文开头"], [])

    if current and _is_nominal_title(current) and current_compact in parent_compact:
        return TitleDecision(current, "valid", "body_heading", ["标题可在父文档正文定位"], [])

    # Web/article titles separated by a pipe are stronger than a later body
    # sentence, provided the last component is a complete nominal title.
    article_parts = [part.strip() for part in re.split(r"[｜|]", original) if part.strip()]
    for candidate in reversed(article_parts):
        if _is_nominal_title(candidate) and not sentence_title_reasons(candidate, has_heading_evidence=True):
            return TitleDecision(candidate, "valid", "web_h1_or_article_title", ["原始网页标题字段"], [])

    quoted_source = re.search(r"《([^》]{4,120}(?:专项应急预案|综合应急预案|事故应急预案|现场处置方案))》", original)
    if quoted_source and not re.match(r"^[（(]?\d+[）)、.．]", original):
        candidate = quoted_source.group(1).strip()
        if _is_nominal_title(candidate):
            return TitleDecision(candidate, "valid", "web_h1_or_article_title", ["原始网页标题中的完整书名号标题"], [])

    if original and _is_nominal_title(original) and not sentence_title_reasons(original, has_heading_evidence=True):
        return TitleDecision(original, "valid", "source_title", ["原始标题为完整名词性标题"], [])

    # A complete title on the first page/body is accepted only if it exists
    # verbatim.  No topic-based synthesis is allowed.
    for line in [line.strip() for line in parent_text[:5000].splitlines() if line.strip()][:80]:
        candidate = line.strip("《》 ")
        if len(candidate) <= 100 and _is_nominal_title(candidate) and not sentence_title_reasons(candidate, has_heading_evidence=True):
            return TitleDecision(candidate, "valid", "cover_or_first_heading", ["父文档前部完整标题"], [])

    filename = Path(source_file).stem
    filename = re.sub(r"-[0-9a-f]{8,}$", "", filename)
    if _is_nominal_title(filename) and not sentence_title_reasons(filename, has_heading_evidence=True):
        return TitleDecision(filename, "valid", "source_filename", ["原始文件名中的完整标题"], [])

    reasons = sentence_title_reasons(original, has_heading_evidence=False)
    warnings = ["sentence_used_as_title"] if reasons else []
    return TitleDecision(original, "uncertain", "source_record", reasons or ["no_reliable_title_evidence"], warnings)


def segment_type_v241(title: str, text: str, parent_type: str, sibling_count: int, title_status: str) -> str:
    title = (title or "").strip()
    probe = f"{title}\n{text[:1800]}"
    if parent_type == "sds":
        return "full_document"
    if re.search(r"SDS附件|安全技术说明书|安全数据表|物質安全資料表", title, re.I):
        return "appendix_sds" if parent_type != "sds" else "embedded_sds"
    if re.search(r"(?:执行|参照|按照).{0,50}(?:专项应急预案|综合应急预案|应急预案)", title) and len(title) <= 180:
        return "reference_sentence"
    sentence_reasons = sentence_title_reasons(title, has_heading_evidence=title_status == "valid")
    if re.match(r"^[（(]?\d+[）)、.]|^[一二三四五六七八九十]+[、.]", title) and (
        sentence_reasons or re.search(r"负责|组织|编制|发布|修订|事故原因|风险原因|立即|采取", title)
    ):
        return "list_fragment"
    if parent_type == "guidance_reference" and (
        re.search(r"(?:公司|企业).{0,35}(?:\d{1,2}[·.月]\d{1,2}|事故)", probe)
        and re.search(r"事故经过|伤亡|死亡|事故原因|直接原因|间接原因|事故教训|人死亡|人受伤|中毒事故", probe)
    ):
        return "accident_case_section"
    if parent_type == "chemical_catalog":
        return "table"
    if re.search(r"现场处置方案|处置卡|岗位处置", title):
        return "onsite_disposal_plan"
    if re.search(r"\.{5,}|…{3,}", title) and re.search(r"\d{1,3}\s*$", title):
        return "toc_fragment"
    if _is_nominal_title(title) and re.search(r"专项应急预案|专项预案", title):
        core = sum(bool(re.search(pattern, text[:8000])) for pattern in (r"适用范围", r"事故风险|危险性分析", r"组织机构|应急组织", r"响应启动|应急响应", r"处置措施|应急处置"))
        if core >= 2:
            return "embedded_special_section"
    if parent_type == "guidance_reference" and sibling_count > 1:
        return "guidance_section"
    if parent_type == "accident_case" and sibling_count > 1:
        return "accident_case_section"
    return "full_document"


def accident_category_v241(title: str, text: str, segment_type: str) -> str:
    if segment_type == "accident_case_section" and re.search(r"中毒|窒息|一氧化碳|硫化氢|缺氧", title + text[:1200]):
        return "poisoning_asphyxia"
    return category_v23(title, text)
