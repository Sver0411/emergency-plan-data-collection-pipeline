from __future__ import annotations

import re
from collections import Counter

from .classification import (
    _evidence, _result, _sds_structure, _severely_garbled, classify,
    content_role_of,
)
from .models import ClassifiedDocumentV22


V23_CATEGORIES = [
    ("mine_transport_accident", r"矿井.{0,20}(?:运输|提升)|煤矿运输|矿山运输"),
    ("natural_disaster", r"自然灾害|防洪|防汛|水灾事故|洪涝|雷电|地震|暴雪|寒潮"),
    ("transport_accident", r"道路运输|车辆运输|运输事故|交通事故|提升运输"),
    ("container_explosion", r"容器爆炸|压力容器事故"),
    ("collapse", r"坍塌|倒塌"),
    ("drowning", r"淹溺|溺水"),
    ("power_accident", r"触电|电力事故|供电中断"),
    ("major_hazard", r"重大危险源"),
    ("confined_space", r"有限空间|受限空间"),
    ("poisoning_asphyxia", r"中毒(?:和|与)?窒息|中毒事故|窒息事故|硫化氢中毒|缺氧窒息"),
    ("chemical_leakage", r"危险化学品泄漏|危化品泄漏|泄漏事故|液氨泄漏|氯气泄漏"),
    ("fire_explosion", r"火灾(?:爆炸)?|爆炸事故"),
]


def category_v23(title: str, text: str) -> str:
    """Title and explicit scope win; only a multi-topic body can yield multi_hazard."""
    title = title or ""
    # An incident heading containing “中毒事故” must beat a parent heading such as
    # “有限空间作业指导手册”.  This prevents the parent topic from leaking into a
    # child accident-case section.
    if re.search(r"(?:中毒|窒息).{0,10}(?:事故|事件)", title) and not re.search(r"(?:泄漏|火灾|爆炸).{0,10}(?:事故|事件)", title):
        return "poisoning_asphyxia"

    body_probe = text[:12000]
    body_topics = {category for category, pattern in V23_CATEGORIES if re.search(pattern, body_probe)}
    # Short guidance/manual paragraphs often enumerate hazards without the full
    # “泄漏事故/中毒事故” phrase.  Use broad terms only for multi-hazard detection;
    # title and scope evidence below still retain precedence for a single category.
    broad_topics = {
        "chemical_leakage": r"泄漏",
        "fire_explosion": r"火灾|爆炸",
        "poisoning_asphyxia": r"中毒|窒息",
        "confined_space": r"有限空间|受限空间",
        "natural_disaster": r"自然灾害|防洪|防汛|水灾|地震|暴雪|寒潮",
        "collapse": r"坍塌|倒塌",
        "transport_accident": r"运输事故|交通事故",
    }
    broad_body_topics = {category for category, pattern in broad_topics.items() if re.search(pattern, body_probe)}
    multi_topics = body_topics | broad_body_topics
    if len(multi_topics) >= 3 and (re.search(r"生产安全事故|危险化学品事故|系统性风险|事故专项应急预案", title) or not title or re.search(r"指导手册|指导意见", title)):
        return "multi_hazard"
    for category, pattern in V23_CATEGORIES:
        if re.search(pattern, title):
            return category
    probes = []
    for marker in [r"适用范围", r"事故风险分析|危险有害因素", r"应急处置|现场处置"]:
        match = re.search(marker, text)
        if match:
            probes.append(text[match.start():match.start() + 900])
    if not probes:
        probes = [text[:3000]]
    mentioned = []
    for category, pattern in V23_CATEGORIES:
        if any(re.search(pattern, probe) for probe in probes):
            mentioned.append(category)
    if len(set(mentioned)) >= 3:
        return "multi_hazard"
    if mentioned:
        return mentioned[0]
    return "other"


def title_from_parent(title: str, segment_text: str, parent_text: str) -> str:
    """Recover a real plan heading when the record starts in a notice/list fragment."""
    title = (title or "").strip()
    notice = re.search(r"已经.{0,30}(?:同意|批准).{0,20}(?:印发|发布)", title + segment_text[:300])
    # A duty-list item is knowledge attached to its parent section, not a
    # heading needing recovery.  Keeping the original text lets the caller
    # merge it back without inventing a new segment title.
    if re.search(r"编制、发布、修订|负责编制|负责组织", title) and not notice:
        return title
    list_fragment = bool(re.match(r"^[（(（]?\d+[）)、.]", title)) or bool(re.match(r"^[一二三四五六七八九十]+[、.]", title))
    if notice or list_fragment or re.search(r"负责|编制、发布、修订|依据列表", title):
        # Keep both quoted titles and real heading lines.  Looking only at quoted
        # references can select the parent comprehensive plan instead of the
        # actual special-plan heading (notably the gas-supply section).
        quoted = re.findall(r"《([^》]{4,80}(?:专项应急预案|应急预案|现场处置方案))》", parent_text)
        headings = re.findall(r"(?m)^\s*(?:\d+[、.．]?\s*)?([^\n]{4,80}(?:专项应急预案|应急预案|现场处置方案))", parent_text)
        candidates = []
        for candidate in quoted + headings:
            candidate = re.sub(r"[\s\u3000]+", "", candidate).strip("。；;，,")
            # Quoted references can be followed by explanatory prose on the
            # same line.  Keep only the first complete plan name so a sentence
            # such as “《…预案》是针对…” never becomes a heading.
            heading_match = re.search(r"《?([^《》\n]{4,100}?(?:专项应急预案|应急预案|现场处置方案))", candidate)
            if heading_match:
                candidate = heading_match.group(1).strip("。；;，,")
            if re.match(r"^(?:[（(]?\d+[）)]?)?(?:制[（(]修|负责|按照|根据|执行|发生|编制|发布|修订)", candidate):
                continue
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        if candidates:
            topic = ""
            for key in ["天然气保供", "危险化学品", "矿井水灾", "生产安全事故"]:
                if key in title + segment_text:
                    topic = key
                    break
            if topic:
                preferred = [candidate for candidate in candidates if topic in candidate]
                if preferred:
                    # Prefer the most specific regional/company heading when
                    # both a city-level reference and the actual child plan
                    # are present in the same document.
                    region_names = sorted(set(re.findall(r"[\u4e00-\u9fff]{2,10}(?:区|县|开发区|园区)", parent_text)), key=len, reverse=True)
                    for region in region_names:
                        regional = [candidate for candidate in preferred if region in candidate]
                        if regional:
                            return min(regional, key=len).strip()
                    exact_special = [candidate for candidate in preferred if re.search(re.escape(topic) + r".{0,18}专项应急预案", candidate)]
                    if exact_special:
                        return min(exact_special, key=len).strip()
                    clean = [candidate for candidate in preferred if "及相关" not in candidate and not candidate.startswith("应")]
                    return max(clean or preferred, key=len).strip()
            return max(candidates, key=len).strip()
    return title


def segment_type_v23(title: str, text: str, parent_type: str, sibling_count: int, parent_text: str = "") -> str:
    title = title.strip()
    if re.search(r"执行[“\"]?.{0,40}(?:专项应急预案|应急预案)", title) or re.match(r"^\d+(?:\.\d+){2,}\s*[^\n]{5,}(?:执行|按照)", title):
        return "reference_sentence"
    if re.match(r"^[（(（]?\d+[）)、.]", title) or re.match(r"^[一二三四五六七八九十]+[、.]", title):
        if re.search(r"负责|组织|编制|发布|修订|通知|贯彻执行", title):
            return "list_fragment"
    if re.search(r"编制、发布、修订|已经.{0,30}(?:同意|批准).{0,20}(?:印发|发布)", title):
        return "reference_sentence"
    if parent_type == "guidance_reference" and re.search(r"(?:\d{1,2}月\d{1,2}日|较大|重大).{0,20}中毒事故|事故（", title + text[:200]):
        return "accident_case_section"
    if parent_type == "chemical_catalog":
        return "table"
    if re.search(r"现场处置方案|处置卡|岗位处置", title):
        return "onsite_disposal_plan"
    if re.search(r"\.{5,}|…{3,}", title) and re.search(r"\d{1,3}\s*$", title):
        return "toc_fragment"
    if parent_type == "guidance_reference" and sibling_count > 1:
        return "guidance_section"
    if parent_type == "accident_case" and sibling_count > 1:
        return "accident_case_section"
    if re.search(r"(?:专项应急预案|专项预案)", title) and (sibling_count > 1 or re.match(r"^(?:第?[一二三四五六七八九十百]+|\d+)[、.．章节\s]", title)):
        return "embedded_special_section"
    if parent_type in {"enterprise_plan", "enterprise_group_plan", "organization_plan", "government_or_regional_plan"} and not re.search(r"应急预案", title) and re.search(r"(?:有限空间|受限空间|危险化学品|重大危险源).{0,20}应急预案", text[:800]):
        return "embedded_special_section"
    return "full_document"


def _special_parent(document_id: str, parent_type: str, role: str, title: str, text: str, category: str, evidence: list[dict], reason: str, review: bool = False) -> ClassifiedDocumentV22:
    result = _result(document_id, parent_type, "full_document", category, role, [reason], evidence, force_review=review)
    return result


def classify_v23(document_id: str, title: str, text: str, source_url: str, original_type: str,
                 *, parent_text: str | None = None, sibling_count: int = 1) -> ClassifiedDocumentV22:
    """V2.3 point fixes layered over the V2.2 parent/segment model; no record-ID answers."""
    parent_text = parent_text or text
    probe = f"{title}\n{parent_text[:120000]}"
    category = category_v23(title, text)

    regulation_mark = _evidence(probe, "regulation_marker", r"规程|规定|办法|条例|部门令|施行日期")
    article_structure = [
        _evidence(probe, "article_first", r"第一条"),
        _evidence(probe, "article_second", r"第二条"),
        _evidence(probe, "article_third", r"第三条"),
    ]
    article_structure = [item for item in article_structure if item]
    if regulation_mark and len(article_structure) >= 2:
        return _special_parent(document_id, "regulation", "regulatory_reference", title, text, "other", [regulation_mark] + article_structure[:2], "正式规程/规定及连续条文结构")

    # Administrative plans are identified before generic “safety measures” or
    # “guidance” phrases that may occur in their appendices.  A named government
    # plan with regional coordination evidence is not a guidance document.
    explicit_plan = bool(re.search(r"专项应急预案|综合应急预案|事故应急预案|现场处置方案", title))
    regional_evidence = [
        _evidence(probe, "government_body", r"人民政府|区政府|市政府|县政府|管委会|安委会|成员单位职责"),
        _evidence(probe, "regional_coordination", r"辖区企业|区域资源调度|请求上级政府|属地管理|各镇人民政府|各街道办事处"),
        _evidence(probe, "regional_plan_title", r"[\u4e00-\u9fff]{2,12}(?:市|区|县|乡镇|街道|开发区|园区).{0,22}(?:应急预案|专项预案)"),
    ]
    regional_evidence = [item for item in regional_evidence if item]
    enterprise_document = bool(re.search(r"(?:编制单位|批准页|预案名称|版本号).{0,120}(?:有限公司|集团公司|集团股份|股份有限公司|煤矿)|(?:有限公司|集团股份有限公司|股份有限公司|煤矿).{0,80}(?:生产安全事故|应急预案)", probe[:5000]))
    if explicit_plan and enterprise_document:
        group_marker = _evidence(probe, "enterprise_group_identity", r"集团公司|集团有限公司|集团股份|集团所属企业|集团应急")
        if group_marker:
            result = _special_parent(document_id, "enterprise_group_plan", "plan_structure_reference", title, text, category, [group_marker], "集团公司/所属企业内部预案证据")
            result.requires_manual_review = False
            return result
    if explicit_plan and len(regional_evidence) >= 2 and not enterprise_document:
        return _special_parent(document_id, "government_or_regional_plan", "plan_structure_reference", title, text, category, regional_evidence, "政府主体、区域计划标题和区域协同证据")

    guidance_markers = [
        _evidence(probe, "guidance_marker", r"指导意见|指导手册|实施解读|新要求解读|国家标准.{0,30}(?:实施|解读)|标准实施"),
        _evidence(probe, "chemical_safety_marker", r"安全措施和应急处置原则"),
    ]
    guidance_markers = [item for item in guidance_markers if item]
    if guidance_markers:
        standard_probe = f"{title}\n{parent_text[:2000]}"
        role = "chemical_safety_reference" if any(item["kind"] == "chemical_safety_marker" for item in guidance_markers) else "standard_interpretation" if re.search(r"实施解读|新要求解读|国家标准.{0,35}(?:正式实施|实施|解读)|标准.{0,20}(?:正式实施|实施解读)", standard_probe) else "guidance_reference"
        extra = _evidence(probe, "guidance_content", r"实施日期|新要求|管理内容|应急处置原则|指导企业")
        if extra: guidance_markers.append(extra)
        result = _special_parent(document_id, "guidance_reference", role, title, text, category, guidance_markers, "明确的指导意见、手册、解读或安全措施参考材料")
        # A document whose own title is an explicit guidance marker has enough
        # type evidence; it remains pending for sampling but is not sent to the
        # uncertainty queue merely because only one title marker was needed.
        if re.search(r"指导意见|指导手册|实施解读|新要求解读|安全措施和应急处置原则", title):
            result.requires_manual_review = False
        return result

    sds_title = _evidence(probe, "sds_title", r"化学品安全技术说明书|化学品安全数据表|安全数据表|安全資料表|物質安全資料表|\bMSDS\b|\bSDS\b")
    sections, sds_evidence = _sds_structure(probe)
    has_identity = bool(re.search(r"(?:化学品(?:中文)?名|化學品名稱|产品名称|中文名称)", probe))
    has_cas = bool(re.search(r"\b\d{2,7}-\d{2}-\d\b", probe))
    if sds_title and len(sections) >= 4 and (has_identity or has_cas):
        evidence = [sds_title] + sds_evidence[:6]
        noisy = _severely_garbled(parent_text)
        result = _special_parent(document_id, "sds", "safety_data", title, text, "other", evidence, f"SDS标识、化学品身份及{len(sections)}类章节结构", review=noisy)
        return result

    base = classify(document_id, title, text, source_url, original_type, parent_text=parent_text, sibling_count=sibling_count)
    # V2.3 adds a broad multi-hazard category only when the body genuinely covers >=3 topics.
    if category != "other":
        base.accident_category = category
    return base
