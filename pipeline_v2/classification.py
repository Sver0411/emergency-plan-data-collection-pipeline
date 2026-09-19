from __future__ import annotations

import re
from collections import Counter

from .models import ClassifiedDocumentV22
from .normalization import looks_garbled


CATEGORY_PATTERNS = [
    ("container_explosion", r"容器爆炸|压力容器事故"),
    ("fire_explosion", r"火灾(?:爆炸)?|爆炸事故"),
    ("natural_disaster", r"自然灾害|防洪|防汛|台风|地震"),
    ("collapse", r"坍塌|倒塌"),
    ("drowning", r"淹溺|溺水"),
    ("transport_accident", r"道路运输|车辆运输|交通事故"),
    ("power_accident", r"触电|电力事故|供电中断"),
    ("major_hazard", r"重大危险源"),
    ("confined_space", r"有限空间|受限空间"),
    ("poisoning_asphyxia", r"中毒(?:和|与)?窒息|窒息事故|硫化氢中毒|缺氧窒息"),
    ("chemical_leakage", r"危险化学品泄漏|危化品泄漏|泄漏事故|液氨泄漏|氯气泄漏"),
]

SDS_TITLE_RE = re.compile(r"化学品安全技术说明书|化学品安全数据表|安全技术说明书|安全数据表|安全資料表|物質安全資料表|\bMSDS\b|\bSDS\b", re.I)
SDS_SECTION_PATTERNS = {
    "identity": r"化学品及企业标识|化學品與廠商資料|化学品标识|化學品名稱",
    "hazards": r"危险性概述|危害辨识|危害辨識",
    "composition": r"成分[/／]?组成信息|成分或组成信息|成分及組成|組成[/／]?成分",
    "first_aid": r"急救措施|急救方法",
    "fire": r"消防措施|滅火措施",
    "spill": r"泄漏应急处理|泄漏处理|洩漏處理方法",
    "handling": r"操作处置与储存|安全處置與儲存方法",
    "ppe": r"接触控制和个体防护|暴露預防措施|個人防護",
    "properties": r"理化特性|物理及化學性質",
    "stability": r"稳定性和反应性|安定性及反應性",
    "toxicology": r"毒理学信息|毒性資料",
    "ecology": r"生态学信息|生態資料",
    "disposal": r"废弃处置|廢棄處置方法",
    "transport": r"运输信息|運送資料",
    "regulatory": r"法规信息|法規資料",
    "other": r"其他信息|其他資料",
}


def _evidence(probe: str, kind: str, pattern: str, strength: str = "strong") -> dict | None:
    match = re.search(pattern, probe, re.I)
    if not match:
        return None
    return {"kind": kind, "text": match.group(0), "start": match.start(), "end": match.end(), "strength": strength}


def _result(document_id: str, parent_type: str, segment_type: str, category: str, role: str,
            reasons: list[str], evidence: list[dict], *, force_review: bool = False,
            confidence_cap: float | None = None) -> ClassifiedDocumentV22:
    evidence = [item for item in evidence if item]
    strong_count = len({item["kind"] for item in evidence if item.get("strength") == "strong"})
    weak_count = len({item["kind"] for item in evidence if item.get("strength") != "strong"})
    if not evidence:
        confidence, parent_type, force_review = 0.3, "manual_review", True
    elif strong_count >= 3:
        confidence = 0.92
    elif strong_count == 2:
        confidence = 0.84
    elif strong_count == 1:
        confidence, force_review = 0.65, True
    elif weak_count:
        confidence, force_review = 0.5, True
    else:
        confidence, force_review = 0.3, True
    if confidence_cap is not None:
        confidence = min(confidence, confidence_cap)
    if confidence <= 0.5:
        force_review = True
    return ClassifiedDocumentV22(
        document_id=document_id, parent_document_type=parent_type, segment_type=segment_type,
        accident_category=category, content_role=role, classification_reasons=reasons,
        classification_evidence=evidence, classification_confidence=confidence,
        requires_manual_review=force_review, review_status="pending",
    )


def category_of(title: str, text: str) -> str:
    """Use title, scope, risk analysis and handling target in order; body words are only a fallback."""
    probes = [title]
    for marker in [r"适用范围", r"事故风险分析|危险有害因素", r"应急处置|现场处置"]:
        match = re.search(marker, text)
        if match:
            probes.append(text[match.start():match.start() + 700])
    for probe in probes:
        for category, pattern in CATEGORY_PATTERNS:
            if re.search(pattern, probe):
                return category
    votes = Counter()
    for category, pattern in CATEGORY_PATTERNS:
        votes[category] = len(re.findall(pattern, text[:2500]))
    if votes and votes.most_common(1)[0][1] >= 2:
        return votes.most_common(1)[0][0]
    return "other"


def _sds_structure(probe: str) -> tuple[list[str], list[dict]]:
    found, evidence = [], []
    for name, pattern in SDS_SECTION_PATTERNS.items():
        item = _evidence(probe, f"sds_section:{name}", pattern)
        if item:
            found.append(name)
            evidence.append(item)
    return found, evidence


def _severely_garbled(text: str) -> bool:
    # Repeated OCR syllables can leave plenty of readable section names while the body is
    # unusable; length and replacement-character ratios alone do not catch this failure.
    replacement_ratio = len(re.findall(r"[�□]", text)) / max(1, len(text))
    repeated_ocr = len(text) > 1000 and bool(re.search(r"([\u4e00-\u9fff]{2,6})\1{3,}", text[:3000]))
    return replacement_ratio > 0.02 or repeated_ocr


def _segment_type(title: str, text: str, parent_type: str, sibling_count: int) -> str:
    title_probe = title.strip()
    if parent_type == "chemical_catalog" and re.search(r"危险化学品(?:目录|名录)", title_probe):
        return "table"
    if re.search(r"\.{5,}|…{3,}", title_probe) and re.search(r"\d{1,3}\s*$", title_probe):
        return "toc_fragment"
    if re.search(r"现场处置方案|处置卡|岗位处置", title_probe):
        return "onsite_disposal_plan"
    if re.search(r"(?:附录|附件|附表)", title_probe):
        return "appendix"
    if parent_type not in {"chemical_catalog", "sds"} and re.fullmatch(r"(?:目录|目\s*录)(?:片段)?", title_probe) and len(text) < 2500:
        return "toc_fragment"
    if re.search(r"(?:专项应急预案|专项预案)", title_probe) and (sibling_count > 1 or re.match(r"^(?:第?[一二三四五六七八九十百]+|\d+)[、.．章节\s]", title_probe)):
        return "embedded_special_section"
    if parent_type in {"enterprise_plan", "enterprise_group_plan", "organization_plan", "government_or_regional_plan"} and not re.search(r"应急预案", title_probe) and re.search(r"(?:有限空间|受限空间|危险化学品|重大危险源).{0,20}应急预案", text[:800]):
        return "embedded_special_section"
    if parent_type in {"enterprise_plan", "enterprise_group_plan", "organization_plan", "government_or_regional_plan"} and (title_probe.endswith(("；", ";", "。")) or re.match(r"^[（(]\d+[）)]", title_probe)) and re.search(r"应急预案", text[:800]):
        return "embedded_special_section"
    if parent_type == "guidance_reference" and sibling_count > 1:
        return "guidance_section"
    if parent_type == "accident_case" and sibling_count > 1:
        return "accident_case_section"
    return "full_document"


def segment_type_of(title: str, text: str, parent_type: str, sibling_count: int) -> str:
    return _segment_type(title, text, parent_type, sibling_count)


def content_role_of(parent_type: str, segment_type: str) -> str:
    if segment_type == "onsite_disposal_plan": return "disposal_knowledge"
    if segment_type == "toc_fragment": return "navigation_only"
    if parent_type == "sds": return "safety_data"
    if parent_type == "chemical_catalog": return "catalog_reference"
    if parent_type == "regulation": return "regulatory_reference"
    if parent_type == "guidance_reference": return "guidance_reference"
    if parent_type == "accident_case": return "accident_case_reference"
    if parent_type in {"enterprise_plan", "enterprise_group_plan", "organization_plan", "government_or_regional_plan"}: return "plan_structure_reference"
    if parent_type == "reject": return "rejected"
    return "manual_review"


def classify(document_id: str, title: str, text: str, source_url: str, original_type: str,
             *, parent_text: str | None = None, sibling_count: int = 1) -> ClassifiedDocumentV22:
    """Classify without consulting document IDs; regression IDs belong only in tests."""
    parent_text = parent_text or text
    probe = f"{title}\n{parent_text[:60000]}"
    category = category_of(title, text)

    sds_title = _evidence(probe, "sds_title", SDS_TITLE_RE.pattern)
    sds_sections, sds_evidence = _sds_structure(probe)
    pubchem = _evidence(probe, "pubchem_aggregate", r"PubChem|PubChem CID")
    catalog_fields = [
        _evidence(probe, "catalog_marker", r"资料类型：危险化学品目录条目|危险化学品目录|危险化学品名录"),
        _evidence(probe, "catalog_index", r"目录序号\s*[:：]"),
        _evidence(probe, "catalog_name", r"(?:品名|名称)\s*[:：]"),
        _evidence(probe, "catalog_hazard", r"危险性类别\s*[:：]"),
    ]
    catalog_evidence = [item for item in catalog_fields if item]
    explicit_plan_title = bool(re.search(r"专项应急预案|综合应急预案|现场处置方案", title))
    if _severely_garbled(parent_text):
        garbled = _evidence(probe, "severe_garbled_text", r"([\u4e00-\u9fff]{2,6})\1{3,}|.{1,20}", "weak")
        return _result(document_id, "reject", "other", category, "rejected", ["内容严重乱码或OCR重复"], [garbled] if garbled else [], force_review=True, confidence_cap=0.5)
    if len(catalog_evidence) >= 3 and not (sds_title and len(sds_sections) >= 4):
        parent_type, reasons, evidence, role = "chemical_catalog", ["具备目录字段结构，且不满足SDS结构"], catalog_evidence, "catalog_reference"
    elif sds_title and len(sds_sections) >= 4 and not pubchem and not explicit_plan_title:
        evidence = [sds_title] + sds_evidence[:5]
        parent_type, reasons, role = "sds", [f"发现明确SDS标识及{len(sds_sections)}类标准章节"], "safety_data"
    elif pubchem:
        evidence = [pubchem, _evidence(probe, "reference_structure", r"Safety and Hazards|GHS Classification", "weak")]
        parent_type, reasons, role = "guidance_reference", ["PubChem聚合资料不是正式供应商SDS"], "chemical_reference"
    else:
        guidance_evidence = [
            _evidence(probe, "guidance_title", r"指导手册|操作手册|作业指导|风险防控|技术指南|操作规范"),
            _evidence(probe, "guidance_process", r"作业前|作业完成|检测方法|操作流程|防控确认"),
        ]
        guidance_evidence = [item for item in guidance_evidence if item]
        accident_evidence = [
            _evidence(probe, "accident_time_or_place", r"\d{1,2}月\d{1,2}日|事故发生(?:时间|地点)|(?:省|市|县|区).{0,20}发生"),
            _evidence(probe, "accident_process", r"事故经过|事故发生经过|事故情况"),
            _evidence(probe, "casualty_or_loss", r"造成\d+人(?:死亡|受伤)|伤亡|直接经济损失"),
            _evidence(probe, "accident_cause", r"事故原因|原因分析|直接原因|间接原因"),
            _evidence(probe, "investigation_or_lesson", r"事故调查|事故通报|教训|防范措施"),
        ]
        accident_evidence = [item for item in accident_evidence if item]
        plan_structure_present = bool(re.search(r"专项应急预案|综合应急预案|应急组织机构|响应启动|应急指挥部", probe[:30000]))
        has_guidance_title = any(item["kind"] == "guidance_title" for item in guidance_evidence)
        if has_guidance_title and not plan_structure_present:
            parent_type, reasons, evidence, role = "guidance_reference", ["指导、操作或风险防控材料"], guidance_evidence, "guidance_reference"
        elif not plan_structure_present and len(accident_evidence) >= 2 and any(item["kind"] in {"accident_time_or_place", "accident_process", "casualty_or_loss"} for item in accident_evidence):
            parent_type, reasons, evidence, role = "accident_case", ["具备事故事实及调查/原因/教训证据"], accident_evidence, "accident_case_reference"
        else:
            regulation_evidence = [
                _evidence(title, "regulation_title", r"(?:条例|办法|规定|标准|规范)(?:（|\(|$)|GB/?T?\s*\d+"),
                _evidence(probe, "issuer_or_article", r"中华人民共和国|国务院|应急管理部|第[一二三四五六七八九十百\d]+条"),
            ]
            regulation_evidence = [item for item in regulation_evidence if item]
            government_evidence = [
                _evidence(probe, "government_body", r"人民政府|管委会|安委会|成员单位职责"),
                _evidence(probe, "regional_coordination", r"辖区企业|区域资源调度|请求上级政府|属地管理"),
                _evidence(probe, "regional_plan_title", r"[\u4e00-\u9fff]{2,12}(?:市|区|县|乡镇|街道|开发区|园区).{0,18}(?:应急预案|专项预案)"),
            ]
            government_evidence = [item for item in government_evidence if item]
            identity_probe = f"{title}\n{parent_text[:2500]}"
            organization_evidence = [
                _evidence(identity_probe, "organization_identity", r"大学|医院|学校|研究院|事业单位|行政机关内部机构|非企业社会组织"),
                _evidence(identity_probe, "organization_internal_scope", r"校内|院内|本校|本院|后勤服务保障单位|动力中心"),
            ]
            organization_evidence = [item for item in organization_evidence if item]
            group_evidence = [
                _evidence(probe, "enterprise_group_identity", r"集团公司|集团有限公司|集团所属企业|适用于集团所属企业"),
                _evidence(probe, "enterprise_group_command", r"集团公司应急救援总指挥|集团应急指挥部|所属企业"),
            ]
            group_evidence = [item for item in group_evidence if item]
            enterprise_evidence = [
                _evidence(probe, "enterprise_identity", r"我公司|本公司|我单位|有限公司|股份有限公司|适用于本公司|生产经营单位"),
                _evidence(probe, "enterprise_site", r"公司厂内|厂区|生产车间|储罐|装置|生产单元"),
                _evidence(probe, "enterprise_command", r"企业内部应急指挥部|公司总经理|生产部门|车间负责人|公司应急指挥部"),
                _evidence(probe, "enterprise_risk", r"公司风险辨识|我公司.{0,30}(?:风险|重大危险源|危险化学品)"),
            ]
            enterprise_evidence = [item for item in enterprise_evidence if item]
            if regulation_evidence and any(item["kind"] == "regulation_title" for item in regulation_evidence) and (original_type == "regulation" or len(regulation_evidence) >= 2) and not plan_structure_present:
                parent_type, reasons, evidence, role = "regulation", ["法规标题及发布/条款结构"], regulation_evidence, "regulatory_reference"
            elif len(government_evidence) >= 2:
                parent_type, reasons, evidence, role = "government_or_regional_plan", ["政府主体及区域协调机制"], government_evidence, "plan_structure_reference"
            elif group_evidence:
                parent_type, reasons, evidence, role = "enterprise_group_plan", ["集团公司及所属企业内部应急机制"], group_evidence, "plan_structure_reference"
            elif len(organization_evidence) >= 2:
                parent_type, reasons, evidence, role = "organization_plan", ["非企业事业/社会组织内部预案"], organization_evidence, "plan_structure_reference"
            elif len(enterprise_evidence) >= 2:
                parent_type, reasons, evidence, role = "enterprise_plan", ["企业身份、生产场景或内部应急机制证据"], enterprise_evidence, "plan_structure_reference"
            elif _severely_garbled(parent_text):
                garbled = _evidence(probe, "garbled_or_too_short", r".{1,20}", "weak")
                return _result(document_id, "reject", "other", category, "rejected", ["内容过短或乱码严重"], [garbled] if garbled else [], force_review=True, confidence_cap=0.5)
            else:
                weak = []
                if original_type:
                    weak.append({"kind": "original_type_hint", "text": original_type, "start": -1, "end": -1, "strength": "weak"})
                return _result(document_id, "manual_review", "other", category, "manual_review", ["规则证据不足"], weak, force_review=True, confidence_cap=0.5)

    segment_type = _segment_type(title, text, parent_type, sibling_count)
    if segment_type == "onsite_disposal_plan":
        role = "disposal_knowledge"
    elif segment_type == "toc_fragment":
        role = "navigation_only"
    elif segment_type in {"embedded_special_section", "full_document"} and parent_type in {"enterprise_plan", "enterprise_group_plan", "organization_plan", "government_or_regional_plan"}:
        role = "plan_structure_reference"
    return _result(document_id, parent_type, segment_type, category, role, reasons, evidence)
