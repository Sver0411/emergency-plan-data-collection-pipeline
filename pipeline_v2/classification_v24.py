from __future__ import annotations

import re
from dataclasses import dataclass

from .classification import _evidence, _result, _sds_structure, _severely_garbled
from .classification_v23 import category_v23, segment_type_v23
from .models import ClassifiedDocumentV22


SDS_FRONT_RE = re.compile(r"化学品安全技术说明书|化学品安全数据表|物質安全資料表|安全資料表|\bSDS\b|\bMSDS\b", re.I)
PLAN_RE = re.compile(r"专项应急预案|综合应急预案|生产安全事故应急预案|现场处置方案")
ENTERPRISE_RE = re.compile(r"有限公司|股份有限公司|集团公司|集团股份|煤矿|本公司|我公司|我单位|本单位|编制单位|公司厂内|厂区|生产车间|储罐区|公司应急救援指挥部")
GOVERNMENT_RE = re.compile(r"人民政府|政府办公室|管委会|应急管理局|行政主管部门|成员单位职责|辖区企业|区域资源调度|请求上级政府")
ORGANIZATION_RE = re.compile(r"大学|医院|学校|研究院|事业单位|动力中心|后勤服务保障单位")


@dataclass
class ParentLock:
    result: ClassifiedDocumentV22
    evidence_scope: str
    embedded_content_types: list[str]
    cover_title: str


def first_effective_title(text: str, fallback: str = "") -> str:
    for line in (line.strip() for line in text[:12000].splitlines() if line.strip()):
        if len(line) > 120 or re.search(r"\.{3,}|…{2,}", line):
            continue
        if PLAN_RE.search(line) or SDS_FRONT_RE.search(line) or re.search(r"规程|规定|办法|条例|指导意见|指导手册|实施解读", line):
            return line.strip("。；;")
    return fallback.strip()


def category_v24(title: str, text: str) -> str:
    """Title/scope first; only explicit multi-topic titles are multi_hazard."""
    title = (title or "").strip()
    title_topics = []
    for name, pattern in [
        ("natural_disaster", r"自然灾害|防洪|防汛|水灾|雷电|地震|暴雪|寒潮"),
        ("transport_accident", r"运输事故|交通事故|车辆伤害"),
        ("mine_transport_accident", r"矿井.{0,20}(?:运输|提升)|煤矿运输"),
        ("container_explosion", r"容器爆炸|压力容器事故"),
        ("collapse", r"坍塌|倒塌"),
        ("drowning", r"淹溺|溺水"),
        ("power_accident", r"触电|电力事故|供电中断"),
        ("major_hazard", r"重大危险源"),
        ("confined_space", r"有限空间|受限空间"),
        ("poisoning_asphyxia", r"中毒(?:和|与)?窒息|中毒事故|窒息事故|硫化氢中毒|缺氧窒息"),
        ("chemical_leakage", r"危险化学品泄漏|危化品泄漏|泄漏事故|液氨泄漏|氯气泄漏"),
        ("fire_explosion", r"火灾|爆炸事故|火灾爆炸"),
    ]:
        if re.search(pattern, title):
            title_topics.append(name)
    if len(set(title_topics)) >= 2:
        return "multi_hazard"
    if title_topics:
        return title_topics[0]
    return category_v23(title, text)


def _special(document_id: str, parent_type: str, role: str, category: str, evidence: list[dict], reason: str, review: bool = False) -> ClassifiedDocumentV22:
    return _result(document_id, parent_type, "full_document", category, role, [reason], evidence, force_review=review)


def lock_parent_type(document_id: str, source_title: str, parent_text: str, original_type: str) -> ParentLock:
    text = parent_text or ""
    front_len = max(3000, min(len(text), int(len(text) * 0.2) or len(text)))
    front = text[:front_len]
    cover = f"{source_title}\n{front}"
    cover_title = first_effective_title(front, source_title)
    scope = "cover"

    regulation = _evidence(cover, "regulation_marker", r"规程|规定|办法|条例|部门令|施行日期")
    articles = [_evidence(cover, f"article_{n}", rf"第{n}条") for n in ("一", "二", "三")]
    articles = [item for item in articles if item]
    if regulation and len(articles) >= 2:
        result = _special(document_id, "regulation", "regulatory_reference", "other", [regulation] + articles[:2], "封面/前部存在正式法规条文结构")
        return ParentLock(result, "cover", _embedded_types(text, "regulation"), cover_title)

    # SDS identity is evaluated on a larger early-document probe than the
    # generic parent front window.  A genuine SDS often places sections 3–6
    # after the supplier block, while a plan appendix may contain SDS text
    # much later.  Requiring an SDS cover title and an SDS-shaped early probe
    # preserves the parent lock and prevents late attachments from winning.
    sds_probe = text[: min(len(text), max(6000, int(len(text) * 0.35)))]
    sds_title = _evidence(sds_probe, "sds_front_title", SDS_FRONT_RE.pattern)
    sds_sections, sds_evidence = _sds_structure(sds_probe)
    identity = bool(re.search(
        r"化学品(?:中文)?名|化學品名稱|产品名称|中文名称|CAS\s*(?:No|号)|"
        r"产品识别者|供應商詳細|供应商详细信息|SDS编号|MSDS编号",
        sds_probe,
        re.I,
    ))
    if sds_title and len(sds_sections) >= 4 and identity and not PLAN_RE.search(sds_probe[:1800]):
        result = _special(document_id, "sds", "safety_data", "other", [sds_title] + sds_evidence[:6], "封面/首部即为SDS并按SDS章节组织", review=_severely_garbled(text))
        return ParentLock(result, "cover", _embedded_types(text, "sds"), cover_title)

    enterprise = _evidence(cover[:9000], "enterprise_cover", r"(?:编制单位|预案名称|发布页|批准页)[\s\S]{0,160}(?:有限公司|股份有限公司|集团公司|集团股份|煤矿)|(?:有限公司|股份有限公司|集团公司|集团股份|煤矿)[\s\S]{0,100}(?:生产安全事故|应急预案)")
    enterprise_scope = _evidence(cover[:9000], "enterprise_scope", r"我公司|本公司|我单位|本单位|公司厂内|厂区|生产车间|储罐区|公司应急救援指挥部|适用于本公司")
    # Issuer evidence is restricted to the cover/early structure.  A hospital,
    # government office or company mentioned in a late appendix is not the
    # document主体.
    organization = _evidence(cover[:3000], "organization_cover", ORGANIZATION_RE.pattern)
    government_title = _evidence(cover[:5000], "government_title", r"[\u4e00-\u9fff]{2,14}(?:市|区|县|乡镇|街道|开发区|园区).{0,28}(?:应急预案|专项预案)")
    government_issuer = _evidence(cover[:5000], "government_issuer", r"人民政府|政府办公室|管委会|行政主管部门")

    if enterprise and (enterprise_scope or PLAN_RE.search(cover_title + "\n" + front[:3000])):
        result = _special(document_id, "enterprise_group_plan" if re.search(r"集团公司|集团股份|集团所属企业", cover[:12000]) else "enterprise_plan", "plan_structure_reference", category_v24(cover_title, front), [item for item in (enterprise, enterprise_scope) if item], "封面、编制单位和企业适用范围优先锁定父文档")
        return ParentLock(result, "cover", _embedded_types(text, result.parent_document_type), cover_title)

    if organization and PLAN_RE.search(cover[:9000]):
        result = _special(document_id, "organization_plan", "plan_structure_reference", category_v24(cover_title, front), [organization], "封面主体为事业/组织单位")
        return ParentLock(result, "cover", _embedded_types(text, "organization_plan"), cover_title)

    if government_title and government_issuer:
        result = _special(document_id, "government_or_regional_plan", "plan_structure_reference", category_v24(cover_title, front), [government_title, government_issuer], "封面标题和发布主体均为行政区域/政府机关")
        return ParentLock(result, "title", _embedded_types(text, "government_or_regional_plan"), cover_title)

    guidance = _evidence(cover[:9000], "guidance_marker", r"指导意见|指导手册|实施解读|新要求解读|安全措施和应急处置原则")
    if guidance and not PLAN_RE.search(cover_title):
        result = _special(document_id, "guidance_reference", "guidance_reference", category_v24(cover_title, front), [guidance], "封面标题明确为指导/解读资料")
        return ParentLock(result, "title", _embedded_types(text, "guidance_reference"), cover_title)

    accident = _evidence(cover[:9000], "accident_case_marker", r"事故通报|事故调查|事故经过|事故原因|伤亡")
    if accident and not PLAN_RE.search(cover_title):
        result = _special(document_id, "accident_case", "accident_case_reference", category_v24(cover_title, front), [accident], "封面/前部为事故通报或调查材料")
        return ParentLock(result, "cover", _embedded_types(text, "accident_case"), cover_title)

    # Fallback is deliberately limited to the front portion, so an attachment
    # or a late appendix cannot overwrite the locked parent type.
    from .classification_v23 import classify_v23

    result = classify_v23(document_id, cover_title, front, "", original_type, parent_text=front, sibling_count=1)
    return ParentLock(result, "dominant_structure", _embedded_types(text, result.parent_document_type), cover_title)


def _embedded_types(text: str, parent_type: str) -> list[str]:
    types: list[str] = []
    if parent_type != "sds" and _find_sds_appendix_start(text) is not None:
        types.append("appendix_sds")
    if re.search(r"专项应急预案", text):
        types.append("embedded_special_section")
    if re.search(r"现场处置方案", text):
        types.append("onsite_disposal_plan")
    return list(dict.fromkeys(types))


def is_sentence_title(title: str) -> bool:
    phrase = re.sub(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|[（(]?\d+(?:\.\d+){0,4}[）)、.．]?)\s*", "", title.strip())
    return bool(re.match(r"^(?:负责|组织|采取|确认|立即|发生|按照|根据|参与|开展|编制|发布|修订|执行|结合|接到|等相关)", phrase)) or phrase.count("，") >= 2 or phrase.endswith(("。", "；", ";"))


def recover_title_v24(title: str, text: str, parent_text: str) -> tuple[str, bool]:
    title = (title or "").strip()
    list_reference = bool(re.match(r"^[（(]?\d+[）)、.．]\s*《", title))
    if title and PLAN_RE.search(title) and not is_sentence_title(title) and not list_reference:
        return title, False
    if not is_sentence_title(title) and not re.match(r"^[（(]?\d+[）)、.]", title):
        return title, False
    candidates = re.findall(r"(?m)^\s*(?:[（(]?\d+[）)、.]?\s*)?([^\n]{4,100}(?:专项应急预案|专项预案|现场处置方案|综合应急预案))", parent_text)
    candidates = [re.sub(r"[\s\u3000]+", "", c).strip("。；;，,") for c in candidates]
    candidates = [c for c in candidates if c and not is_sentence_title(c)]
    probes = f"{title}\n{text[:1200]}"
    if list_reference:
        quoted = [
            re.sub(r"[\s\u3000]+", "", c).strip("。；;，,")
            for c in re.findall(r"《[^》]{4,140}(?:专项应急预案|专项预案|事故应急预案|应急预案)》", parent_text)
        ]
        quoted = [c for c in quoted if c and not is_sentence_title(c)]
        # A quoted full title in the same parent is stronger than a numbered
        # reference-list item.  Prefer the title whose topic is visible in the
        # selected row/parent prefix (for example, the Yangpu chemical-plan
        # title over the referenced municipal plan).
        for key in ("危险化学品", "天然气保供", "有限空间", "重大危险源", "电力生产"):
            selected = [c for c in quoted if key in c and key in probes]
            if selected:
                return min(selected, key=len), True
        if quoted:
            return min(quoted, key=len), True
    # Prefer a candidate whose topic is actually present in the selected
    # record/title.  Looking across every parent candidate without this gate
    # can recover an unrelated later “有限空间” section for a gas-supply row.
    for key in ("天然气保供", "电力生产", "危险化学品泄漏", "重大危险源", "受限空间", "有限空间"):
        if key not in probes:
            continue
        selected = [c for c in candidates if key in c]
        if selected:
            return min(selected, key=len), True
    if candidates:
        return min(candidates, key=len), True
    if re.search(r"有限空间|受限空间", probes):
        return "有限空间事故应急预案", True
    return title, False


def _find_sds_appendix_start(text: str) -> int | None:
    if not text:
        return None
    pattern = re.compile(r"(?m)^\s*(?:[（(]?\d+[）)、.]?\s*)?(?:氢氧化钠|盐酸|硫酸|硝酸|氨水|甲醇|乙醇|苯|甲醛|氯气|硫化氢)[^\n]{0,40}\n(?:化学品中文名|中文名|化學品名稱|CAS\s*(?:No|号))", re.I)
    match = pattern.search(text)
    if match:
        return match.start()
    generic = re.search(r"(?m)^\s*(?:化学品中文名|化学品名称|化學品名稱)\s*[:：]", text)
    return generic.start() if generic else None


def segment_type_v24(title: str, text: str, parent_type: str, sibling_count: int) -> str:
    if re.search(r"SDS附件|安全数据表|安全技术说明书|物質安全資料表", title, re.I):
        # A standalone SDS is already the locked parent document.  The
        # appendix/embedded labels are reserved for SDS material found inside
        # a non-SDS parent and therefore must never change that parent type.
        return "appendix_sds" if parent_type != "sds" else "full_document"
    if re.search(r"执行[“\"]?.{0,40}(?:专项应急预案|应急预案)", title) or is_sentence_title(title):
        return "reference_sentence" if re.search(r"执行|接到|按照", title) else "list_fragment"
    if re.search(r"现场处置方案|处置卡|岗位处置", title):
        return "onsite_disposal_plan"
    if parent_type == "chemical_catalog":
        return "table"
    if re.search(r"\.{5,}|…{3,}", title):
        return "toc_fragment"
    if re.search(r"专项应急预案|专项预案", title):
        return "embedded_special_section" if sibling_count > 1 or len(title) < 100 else "embedded_special_section"
    if parent_type == "guidance_reference" and sibling_count > 1:
        return "guidance_section"
    if parent_type == "accident_case" and sibling_count > 1:
        return "accident_case_section"
    return "full_document"
