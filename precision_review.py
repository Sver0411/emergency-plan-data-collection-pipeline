#!/usr/bin/env python3
"""第二轮离线审核：重分预案层级并制作 10 种化学品的 SDS 审核样例。

本脚本只读取 cleaned/ 下的一轮结果，所有新文件写入 review_round2/。
它不会联网、不会修改原始 JSON，也不会生成预案模板或导入知识库。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CLEANED = ROOT / "cleaned"
OUTPUT = ROOT / "review_round2"


# 此清单是按文档标题、正文的适用范围和发布/编制主体逐一复核后的结果；
# 不能用一轮评分替代该判断，因此保留在脚本中以便复现审查结论。
PLAN_LEVEL_BY_URL_TITLE: dict[tuple[str, str], tuple[str, str]] = {
    ("https://www.zhenping.gov.cn/ifile/20260123/17691396329583m3TI3cm.pdf", "14 第三方事故威胁我方管道事件现场处置方案"): ("onsite_disposal_plan", "标题明确为现场处置方案，仅保留作具体处置措施来源。"),
    ("https://www.huarong.gov.cn/uploadfiles/202310/2023100808463339405.pdf", "4.3.2.3 泄漏事故现场处置要点"): ("onsite_disposal_plan", "为处置要点章节，不是独立企业专项预案。"),
    ("https://www.shyp.gov.cn/zhengwu/zwgk-zdjc2024/2025/219/b90f94247ec1466f3a593284c23c194a.html", "制定《上海市杨浦区处置危险化学品生产安全事故应急预案》 2024-03-27"): ("government_or_regional_plan", "区级政府预案，用于组织与响应机制参考。"),
    ("https://www.bjft.gov.cn/ftq/yjgl/202308/a16e2423f2ca4225a1fb73b787e6cba3.shtml", "丰台区危险化学品事故应急预案"): ("government_or_regional_plan", "区级政府预案。"),
    ("https://www.sxakhidz.gov.cn/Content-2791124.html", "危险化学品泄漏事故专项应急预案"): ("government_or_regional_plan", "开发区管委会编制的园区预案。"),
    ("https://www.pudong.gov.cn/zwgk/14528.gkml_ywl_yjgl/2022/316/296897.html", "关于印发《上海市浦东新区惠南镇危险化学品事故专项应急预案》的通知"): ("government_or_regional_plan", "镇级政府预案。"),
    ("https://www.rongchang.gov.cn/zwgk_264/zcwj/qzfwj/zcyy/202111/t20211117_9988687.html", "《重庆市荣昌区危险化学品事故专项应急预案》已经区政府同意，现印发给你们，请认真贯彻执行。"): ("government_or_regional_plan", "区政府印发的区域预案。"),
    ("https://xhq.nc.gov.cn/xhqrmzf/yjya01/202104/cb67fd3b1329456d9829d2105eb80654.shtml", "西湖区危险化学品生产安全事故应急预案"): ("government_or_regional_plan", "区级政府预案。"),
    ("https://www.yuanling.gov.cn/yuanling/c108890/202508/c121e5e76be34805a92c7f069142f826/files/a0bc72cc25484863a97cb0aa160a9998.pdf", "三、危险化学品事故专项应急预案"): ("government_or_regional_plan", "开发区管委会区域预案。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "1 危险化学品事故专项应急预案"): ("government_or_regional_plan", "镇域辖区预案，不是单一企业预案。"),
    ("https://www.yuanling.gov.cn/yuanling/c108890/202508/c121e5e76be34805a92c7f069142f826/files/a0bc72cc25484863a97cb0aa160a9998.pdf", "四、燃气泄漏事故专项应急预案"): ("government_or_regional_plan", "开发区层级预案，且正文含具体企业资料，不作模板候选。"),
    ("https://www.huaishang.gov.cn/zfxxgk/public/24521/48127231.html", "梅桥镇液氨事故应急预案"): ("government_or_regional_plan", "乡镇预案。"),
    ("https://xxgk.yczf.gov.cn/qtdw_42136/ycxjjjskfq/fdzdgknr/fgwj/202509/P020251223390182355986.pdf", "四、危险化学品车辆运输事故专项应急预案"): ("government_or_regional_plan", "化工园区车辆运输预案。"),
    ("https://xxgk.yczf.gov.cn/qtdw_42136/ycxjjjskfq/fdzdgknr/fgwj/202509/P020251223390182355986.pdf", "二、危险化学品泄漏事故专项应急预案"): ("government_or_regional_plan", "化工园区预案。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "2 危险化学品道路运输事故专项应急预案"): ("government_or_regional_plan", "镇域道路运输预案。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "2.4.2 危险化学品道路运输事故专项应急预案响应程序"): ("reject", "原预案中的响应程序片段，缺少完整预案结构。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "2.1 气体类危险化学品爆炸燃烧事故现场处置方案要点"): ("onsite_disposal_plan", "处置方案要点，适合提取具体措施。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "3.1 气体类危险化学品泄漏事故现场处置方案要点"): ("onsite_disposal_plan", "处置方案要点，适合提取具体措施。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "2.2 液体类危险化学品爆炸燃烧事故现场处置方案要点"): ("onsite_disposal_plan", "处置方案要点，适合提取具体措施。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "3.2 液体类危险化学品泄漏事故现场处置方案要点"): ("onsite_disposal_plan", "处置方案要点，适合提取具体措施。"),
    ("https://ankebio.com/upload/aqyjyba.pdf", "危险化学品泄漏事故专项应急预案"): ("enterprise_special_plan", "正文以‘我单位’和具体危化品清单界定适用范围，属于企业专项预案。"),
    ("https://www.rugao.gov.cn/rgsxyz/upload/b0e59cca-ebdd-46c0-8c25-9c73ef57b188.pdf", "4 有限空间作业专项应急预案"): ("government_or_regional_plan", "适用范围为镇内生产经营单位，属于区域预案。"),
    ("https://www.huarong.gov.cn/uploadfiles/202310/2023100808463339405.pdf", "十 华容县交通运输受限空间突发事故专项应急预案 .............183"): ("government_or_regional_plan", "县级交通运输行业预案；目录噪声在章节重提取时排除。"),
    ("https://innovation.jinan.gov.cn/cms_files/jcms1/web46/site/attach/0/6030b29d45744adeb14ddecec3c0941b.pdf?fileName=6030b29d45744adeb14ddecec3c0941b.pdf", "2 有限空间事故专项应急预案"): ("government_or_regional_plan", "街道辖区预案。"),
    ("https://www.xuanhan.gov.cn/xxgk-show-84688.html", "现将《五宝镇2025年有限空间作业安全事故应急预案》印发给你们，请结合工作实际抓好贯彻落实。"): ("government_or_regional_plan", "镇政府印发预案。"),
    ("https://www.zhenping.gov.cn/ifile/20260123/17691396329583m3TI3cm.pdf", "8 有限空间事故专项应急预案"): ("enterprise_special_plan", "正文明确适用于河南省发展燃气有限公司生产系统，属于企业专项预案。"),
    ("https://www.sdclny.com/uploads/allimg/201108/%E6%BB%95%E5%B7%9E%E5%B8%82%E4%B8%9C%E5%A4%A7%E7%9F%BF%E4%B8%9A%E6%9C%89%E9%99%90%E8%B4%A3%E4%BB%BB%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E7%8E%B0%E5%9C%BA%E5%A4%84%E7%BD%AE%E6%96%B9%E6%A1%88.pdf", "15.有限空间作业事故现场处置方案"): ("onsite_disposal_plan", "企业现场处置方案，不作为企业专项预案模板样本。"),
    ("https://oss.lcweb01.cn/jzt/3318/file/20240819/247959232e983b5ec3255bf007f1a000.pdf", "9、受限空间作业事故专项应急预案"): ("enterprise_special_plan", "正文明确为鑫途化工股份有限公司厂区的专项预案。"),
    ("https://www.sinolube.com/Upload/file/20211104/20211104102915_3349.pdf", "九、有限空间作业事故现场处置方案"): ("onsite_disposal_plan", "标题及内容均为现场处置方案。"),
    ("https://www.sdclny.com/static/upload/file/20240521/1716282342307372.pdf", "14 有限空间作业现场处置方案"): ("onsite_disposal_plan", "标题明确为现场处置方案。"),
    ("https://innovation.jinan.gov.cn/cms_files/jcms1/web46/site/attach/0/6030b29d45744adeb14ddecec3c0941b.pdf?fileName=6030b29d45744adeb14ddecec3c0941b.pdf", "9 中毒窒息事故专项应急预案"): ("government_or_regional_plan", "街道辖区预案。"),
    ("https://www.weidong.gov.cn/upload/files/2021/11/2316519631.pdf", "2.窒息事故现场处置要点"): ("onsite_disposal_plan", "现场处置要点；报告中标记了‘小动物试验’等不应复用的内容。"),
    ("https://www.yuanling.gov.cn/yuanling/c108890/202508/c121e5e76be34805a92c7f069142f826/files/a0bc72cc25484863a97cb0aa160a9998.pdf", "五、职业病危害事故专项应急预案"): ("government_or_regional_plan", "开发区预案，且主题宽于中毒窒息。"),
    ("https://hnls.gov.cn/upload/files/2021/12/1618157181.pdf", "2.窒息事故现场处置要点"): ("onsite_disposal_plan", "现场处置要点；报告中标记了‘小动物试验’等不应复用的内容。"),
    ("https://www.yncdc.cn/UploadFile/bf2009/cdc727391.pdf", "非职业性一氧化碳中毒事件应急预案"): ("government_or_regional_plan", "公共卫生事件预案，仅作跨部门响应机制参考。"),
    ("https://xxgk.yczf.gov.cn/qtdw_42136/ycxjjjskfq/fdzdgknr/fgwj/202509/P020251223390182355986.pdf", "三、中毒和窒息事故专项应急预案"): ("government_or_regional_plan", "化工园区预案。"),
    ("https://www.sdclny.com/static/upload/file/20240521/1716282342307372.pdf", "4 瓦斯事故现场处置方案"): ("onsite_disposal_plan", "矿井瓦斯现场处置方案，仅可作特定场景措施参考。"),
    ("https://www.sdclny.com/uploads/allimg/201108/%E6%BB%95%E5%B7%9E%E5%B8%82%E4%B8%9C%E5%A4%A7%E7%9F%BF%E4%B8%9A%E6%9C%89%E9%99%90%E8%B4%A3%E4%BB%BB%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E7%8E%B0%E5%9C%BA%E5%A4%84%E7%BD%AE%E6%96%B9%E6%A1%88.pdf", "11.职业病危害事故现场处置方案"): ("reject", "主题为综合职业病危害，不能可靠代表中毒窒息专项预案。"),
    ("https://www.yuanling.gov.cn/yuanling/c108890/202508/c121e5e76be34805a92c7f069142f826/files/a0bc72cc25484863a97cb0aa160a9998.pdf", "五、职业病危害事故专项应急预案"): ("government_or_regional_plan", "开发区预案，且主题宽于中毒窒息。"),
    ("https://oss.lcweb01.cn/jzt/3318/file/20240819/247959232e983b5ec3255bf007f1a000.pdf", "1 硫回收单元和反应分离单元硫化氢中毒现场处置方案"): ("onsite_disposal_plan", "企业现场处置方案，适合提取硫化氢处置步骤。"),
    ("https://www.huasugroup.com/sites/default/files/%E8%8B%8F%E5%B7%9E%E5%8D%8E%E8%8B%8F%E5%A1%91%E6%96%99%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E7%94%9F%E4%BA%A7%E5%AE%89%E5%85%A8%E4%BA%8B%E6%95%85%E5%BA%94%E6%80%A5%E6%95%91%E6%8F%B4%E9%A2%84%E6%A1%88%EF%BC%882021.6%E6%9C%80%E6%96%B0%EF%BC%89.pdf", "7、中毒窒息事故专项应急预案"): ("enterprise_special_plan", "正文明确 PVC 生产场景，属于企业中毒窒息专项预案。"),
}


def normalize(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", text.replace("\r", "\n")).strip()


def redact_sensitive(text: str) -> tuple[str, list[str]]:
    """仅在生成的候选副本中脱敏；绝不回写原始文件。"""
    rules: list[tuple[str, str]] = [
        (r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[已脱敏电话]"),
        (r"(?<!\d)0\d{2,3}[-— ]?\d{7,8}(?!\d)", "[已脱敏电话]"),
        (r"(?im)(?:联系人|应急联系人|现场联系人|企业联系人)\s*[：:]?\s*[\u4e00-\u9fff]{2,4}", lambda m: re.split(r"[：:]", m.group(0))[0] + "：[已脱敏联系人]"),
        (r"(?im)((?:办公|企业|单位|厂区|厂|联系)?地址\s*[：:]?)[^\n；;。]{5,80}", r"\1[已脱敏地址]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[已脱敏邮箱]"),
    ]
    notes: list[str] = []
    result = text
    for pattern, repl in rules:
        result, count = re.subn(pattern, repl, result)
        if count:
            notes.append(f"{count} 处敏感信息已脱敏")
    return result, notes


def is_heading(line: str) -> bool:
    compact = normalize(line)
    if not compact or len(compact) > 90:
        return False
    if re.search(r"\.{3,}\s*\d+\s*$", compact):
        return False  # 目录引导符
    return bool(re.match(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)\s*\S+", compact))


def section_id(heading: str, ordinal: int) -> str:
    match = re.match(r"\s*(第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十]+|\d+(?:\.\d+)*)", heading)
    return match.group(1) if match else f"section_{ordinal:02d}"


def extract_sections(content: str, template_candidate: bool) -> tuple[list[dict[str, str]], list[str]]:
    """按文本中真实标题切分，标题始终与其下正文作为同一 section 输出。"""
    content = content.replace("\r", "\n")
    raw_lines = [normalize(line) for line in content.split("\n")]
    lines = [line for line in raw_lines if line and line not in {"目录", "目 录"} and not re.search(r"\.{3,}\s*\d+\s*$", line)]
    sections: list[dict[str, str]] = []
    active_heading: str | None = None
    active_lines: list[str] = []

    def flush() -> None:
        nonlocal active_heading, active_lines
        if not active_heading:
            return
        body = "\n".join(active_lines).strip()
        # 只有标题没有正文的目录碎片不进入结构化结果。
        if len(re.sub(r"\s+", "", body)) >= 25:
            if template_candidate:
                body, _ = redact_sensitive(body)
                safe_heading, _ = redact_sensitive(active_heading)
            else:
                safe_heading = active_heading
            sections.append({"section_id": section_id(safe_heading, len(sections) + 1), "heading": safe_heading, "content": body})
        active_heading, active_lines = None, []

    for line in lines:
        if is_heading(line):
            flush()
            active_heading = line
            active_lines = []
        elif active_heading:
            active_lines.append(line)
        else:
            # 正文开头没有可识别编号时，保留为前言，避免标题与首段脱节。
            active_heading = "前言/适用范围"
            active_lines = [line]
    flush()
    if not sections and lines:
        fallback = "\n".join(lines)
        if template_candidate:
            fallback, _ = redact_sensitive(fallback)
        sections = [{"section_id": "section_01", "heading": "正文", "content": fallback}]
    return sections, []


def plan_record(record: dict[str, Any], level: str, reason: str) -> dict[str, Any]:
    candidate = level == "enterprise_special_plan"
    title = record["title"]
    title_notes: list[str] = []
    if candidate:
        title, title_notes = redact_sensitive(title)
    sections, _ = extract_sections(record.get("content", ""), candidate)
    result: dict[str, Any] = {
        "title": title,
        "category": record.get("category", ""),
        "plan_level": level,
        "sections": sections,
        "source_url": record.get("source_url", ""),
        "review_reason": reason,
    }
    if candidate:
        result["redaction_notes"] = title_notes + ["候选正文按联系人、电话、地址、邮箱规则脱敏；原始文件未修改。"]
    return result


def field(source_url: str, text: str) -> str:
    return f"{text}（来源：{source_url}）"


def sds_pilot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {row.get("chemical"): row for row in rows}
    # 仅使用现有本地合并记录中可追溯的段落，且不把 PubChem 或 SDS 全文作为条目写出。
    profiles: dict[str, dict[str, Any]] = {
        "甲醇": {
            "record_name": "甲醇", "display": "甲醇", "cas": "67-56-1",
            "hazards": ["当前记录为含甲醇的分析标准混合物；其标注为高度易燃、吸入有毒并会刺激皮肤和眼睛。"],
            "spill_response": ["佩戴呼吸防护后使未受防护人员远离；以吸收材料收集泄漏物并保持通风。"],
            "ppe": ["使用防护手套、防护服、护目/面部防护及适用呼吸防护。"],
            "first_aid": ["吸入后移至新鲜空气或供氧并求医；皮肤用水和肥皂彻底冲洗；眼睛以流水冲洗后持续不适则就医。"],
            "incompatible": [], "environment": ["防止进入下水道、地表水或地下水。"],
            "forbidden": ["远离热源、火花和明火；禁止吸烟。"],
            "notes": ["所选来源是含约 98.7% 甲醇与约 1.26% 苯酚的混合物，不应作为纯甲醇的最终 SDS；本轮仅作保守样例。"],
        },
        "乙醇": {
            "record_name": "乙醇", "display": "乙醇", "cas": "",
            "hazards": ["现有来源的聚合 GHS 信息显示：高度易燃液体和蒸气；部分申报还提示严重眼刺激。"],
            "spill_response": [], "ppe": [], "first_aid": [], "incompatible": [], "environment": [],
            "forbidden": ["远离热源、火花和明火；避免静电放电。"],
            "notes": ["现有本地条目只有 PubChem 聚合来源，未发现可复核的供应商 SDS；CAS 在现有合并记录为空，按要求不补猜。"],
        },
        "苯": {
            "record_name": None, "display": "苯", "cas": "",
            "hazards": [], "spill_response": [], "ppe": [], "first_aid": [], "incompatible": [], "environment": [], "forbidden": [],
            "notes": ["现有 sds_merged.json 未识别到名称为‘苯’的有效条目；不以氯苯、甲苯或其他含‘苯’化合物替代，也不猜测 CAS。"],
        },
        "盐酸": {
            "record_name": "盐酸", "display": "盐酸", "cas": "7647-01-0",
            "hazards": [], "spill_response": [], "ppe": [], "first_aid": [], "incompatible": [], "environment": [], "forbidden": [],
            "notes": ["当前选中来源的标题/合并名称为盐酸，但正文混入乙酸乙酯等不一致组分和性质；为避免错配，本轮不提取其安全措施，待逐页人工复核后再入库。"],
        },
        "硫酸": {
            "record_name": "硫酸", "display": "硫酸", "cas": "7664-93-9",
            "hazards": ["不燃但强腐蚀；可造成严重皮肤灼伤和眼损伤；遇水大量放热并可能沸溅。"],
            "spill_response": ["划定警戒区并从侧风、上风向撤离无关人员；在适当防护下切断泄漏源。", "小量以干燥砂土或其他不燃材料覆盖收集；大量泄漏构筑围堤/挖坑收容，并按来源建议使用适当中和材料。"],
            "ppe": ["应急处理人员使用正压自给式呼吸器和防酸碱服；作业人员使用适用呼吸、眼面、耐酸碱手部和身体防护。"],
            "first_aid": ["皮肤接触后立即脱去污染衣物并用水清洗/淋浴；眼睛用水持续冲洗；吸入者移至空气新鲜处并立即求医；误食漱口且不得诱导呕吐。"],
            "incompatible": ["避免与易燃/可燃物及来源列举的强反应性物质接触。"],
            "environment": ["防止通过下水道、通风系统和密闭空间扩散。"],
            "forbidden": ["不得让泄漏物接触木材、纸、油等可燃物；避免水流直接冲击泄漏物。"],
            "notes": [],
        },
        "硝酸": {
            "record_name": "硝酸", "display": "硝酸", "cas": "7697-37-2",
            "hazards": ["可造成严重皮肤灼伤和眼损伤。"],
            "spill_response": ["隔离泄漏区域，人员撤至上风向；保证通风并在适当防护下处理。", "确保木材、纸、油等可燃物远离泄漏物；避免排放到周围环境。"],
            "ppe": ["使用防腐蚀护目镜、耐酸碱化学防护手套、适用呼吸防护用品和抗酸碱防护服。"],
            "first_aid": ["眼睛以大量水冲洗并立即就医；皮肤脱去污染衣物后以大量水冲洗；吸入后转移至新鲜空气并立即医疗处置；误食不得催吐。"],
            "incompatible": ["来源提示避免接触有机物、燃料、溶剂、锯屑、纸张和衣料等禁忌物。"],
            "environment": ["在安全条件下防止进一步泄漏或溢出，并避免排放至周围环境。"],
            "forbidden": ["不得触摸或跨越泄漏物；未穿合适防护服不得接触破损容器或泄漏物。"],
            "notes": ["该来源的部分泄漏段落存在通用化措辞；仅保留与腐蚀性/氧化性风险一致的措施，仍需人工复核产品浓度。"],
        },
        "氢氧化钠": {
            "record_name": "氢氧化钠", "display": "氢氧化钠", "cas": "1310-73-2",
            "hazards": ["具有强腐蚀性，可造成严重皮肤灼伤和眼损伤；遇水/水蒸气大量放热，潮湿时与铝、锌、锡反应可放出氢气。"],
            "spill_response": ["隔离泄漏污染区并限制出入；穿好适当防护服后处理，尽可能切断泄漏源。", "以洁净铲子收集泄漏物，置于干净、干燥且盖子较松的容器后移离泄漏区。"],
            "ppe": ["可能接触粉尘时使用过滤式防尘呼吸器；佩戴化学安全防护眼镜、橡胶耐酸碱服和橡胶耐酸碱手套。"],
            "first_aid": ["皮肤接触后立即脱去污染衣物并用水清洗/淋浴；眼睛用水冲洗；吸入后转移至空气新鲜处并求医；误食漱口且不得诱导呕吐。"],
            "incompatible": ["强酸、易燃或可燃物、二氧化碳、过氧化物和水。"],
            "environment": ["覆盖泄漏物以减少飞散，并避免使水进入包装容器。"],
            "forbidden": ["稀释或配制溶液时应把碱加入水中；避免与酸类接触和让包装受潮。"],
            "notes": [],
        },
        "氨": {
            "record_name": "氨", "display": "氨", "cas": "7664-41-7",
            "hazards": ["易燃液化加压气体；吸入有毒，可造成严重皮肤灼伤和眼损伤，对水生生物毒性极大。"],
            "spill_response": ["撤离现场并禁止未授权人员进入；通风不良区域处理时使用自给式空气呼吸器，消除点火源并通风。", "在确保安全时堵塞泄漏，使用不产生火花的清洁工具收集吸收材料，相关设备接地。"],
            "ppe": ["使用呼吸防护装置、耐化学品橡胶手套、护目/面部防护和防护服；反复或长期处理时穿防渗透衣物与靴子。"],
            "first_aid": ["吸入后转移至空气新鲜处并联系解毒中心或医生；皮肤/眼睛接触立即持续冲洗并就医；误食漱口且不得诱导呕吐。"],
            "incompatible": ["酸类、氧化剂类、醇类和金属。"],
            "environment": ["防止溢出物流入下水道、河道或低洼区域。"],
            "forbidden": ["漏气着火时，除非能够安全制止泄漏，否则不要灭火；远离热源、火花和明火。"],
            "notes": [],
        },
        "氯气": {
            "record_name": "氯", "display": "氯气", "cas": "7782-50-5",
            "hazards": ["黄绿色强刺激性加压气体；吸入可致命，可造成皮肤和眼损伤；不燃但可助燃，对水生生物毒性很高。"],
            "spill_response": ["划定警戒区，无关人员从侧风、上风向撤离；应急人员使用内置正压自给式呼吸器的全封闭防化服。", "尽可能切断泄漏源，避免水流直接冲击泄漏物；保持泄漏场所通风并隔离至气体散尽。"],
            "ppe": ["浓度超标时佩戴过滤式防毒面具；紧急抢救或撤离时使用空气呼吸器，配合密闭型防毒服和橡胶手套。"],
            "first_aid": ["吸入后迅速移至空气新鲜处，必要时给氧或心肺复苏并就医；皮肤用水充分清洗；眼睛以流动清水或生理盐水冲洗并就医。"],
            "incompatible": ["易燃/可燃物、烃类、炔烃、醇类、乙醚、氢、金属、苛性碱等来源列举禁配物。"],
            "environment": ["防止气体通过下水道、通风系统和密闭空间扩散。"],
            "forbidden": ["禁止接触或跨越泄漏物；禁止用水直接冲击泄漏物或泄漏源；避免与可燃物接触。"],
            "notes": ["现有合并名称为‘氯’，本输出按常用名映射为‘氯气’，未改变 CAS 或来源。"],
        },
        "硫化氢": {
            "record_name": "硫化氢", "display": "硫化氢", "cas": "7783-06-4",
            "hazards": ["易燃有毒加压气体；吸入可致死，对水生生物毒性很高；高浓度暴露可迅速导致意识障碍、呼吸和循环衰竭。"],
            "spill_response": ["撤离泄漏污染区人员至上风处并隔离，切断火源；应急人员使用自给正压式呼吸器和防静电工作服。", "尽可能切断泄漏源并通风加速扩散；收容泄漏物，防止进入下水道、地表水和地下水。"],
            "ppe": ["浓度超标时使用过滤式防毒面具；紧急抢救或撤离使用氧气/空气呼吸器，并配合耐腐蚀手套、化学防护眼镜和防静电工作服。"],
            "first_aid": ["吸入后移至空气新鲜处、保持呼吸道通畅，必要时给氧；呼吸停止时进行人工呼吸并就医。皮肤/眼睛接触立即充分冲洗并就医。"],
            "incompatible": ["强氧化剂、碱类。"],
            "environment": ["防止泄漏物进入下水道、地表水和地下水。"],
            "forbidden": ["远离明火和高热；漏气着火时，不能安全切断气源则不应熄灭泄漏处火焰。"],
            "notes": [],
        },
    }
    output: list[dict[str, Any]] = []
    for key, profile in profiles.items():
        row = by_name.get(profile["record_name"]) if profile["record_name"] else None
        urls = row.get("source_urls", []) if row else []
        source_url = row.get("source_url", "") if row else ""
        # 仅把简短、可核对的摘录写入各字段；空字段表示现有来源不足，而不是自动补全。
        def tagged(items: list[str]) -> list[str]:
            return [field(source_url, item) for item in items] if source_url else []
        output.append({
            "chemical_name": profile["display"],
            "cas": profile["cas"] if row else "",
            "hazards": tagged(profile["hazards"]),
            "spill_response": tagged(profile["spill_response"]),
            "ppe": tagged(profile["ppe"]),
            "first_aid": tagged(profile["first_aid"]),
            "incompatible_materials": tagged(profile["incompatible"]),
            "environmental_precautions": tagged(profile["environment"]),
            "forbidden_actions": tagged(profile["forbidden"]),
            "source_urls": urls,
            "conflict_notes": profile["notes"],
            "review_status": "pending",
        })
    return output


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    plans = json.loads((CLEANED / "selected_plan_samples.json").read_text(encoding="utf-8"))
    sds_rows = json.loads((CLEANED / "sds_merged.json").read_text(encoding="utf-8"))
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decisions: list[tuple[dict[str, Any], str, str]] = []
    # 同标题、同 URL 的记录只保留正文最长的一条；其余为同源截断重复。
    longest_by_key: dict[tuple[str, str], int] = {}
    for plan in plans:
        key = (plan.get("source_url", ""), plan.get("title", ""))
        longest_by_key[key] = max(longest_by_key.get(key, 0), len(plan.get("content", "")))
    for plan in plans:
        key = (plan.get("source_url", ""), plan.get("title", ""))
        if len(plan.get("content", "")) < longest_by_key[key]:
            level, reason = ("reject", "与同标题、同 URL 的较完整记录重复，且正文更短；保留完整记录。")
        else:
            level, reason = PLAN_LEVEL_BY_URL_TITLE.get(key, ("reject", "未命中复核清单，保守拒绝，避免未审核样本进入候选集。"))
        decisions.append((plan, level, reason))
        groups[level].append(plan_record(plan, level, reason))

    # 四个文件均为新副本，原始一轮文件保持不变。
    write_json(OUTPUT / "template_candidate_plans.json", groups["enterprise_special_plan"])
    write_json(OUTPUT / "government_reference_plans.json", groups["government_or_regional_plan"])
    write_json(OUTPUT / "disposal_knowledge_sources.json", groups["onsite_disposal_plan"])
    rejected = []
    for plan, level, reason in decisions:
        if level == "reject":
            rejected.append({"title": plan.get("title", ""), "category": plan.get("category", ""), "source_url": plan.get("source_url", ""), "plan_level": "reject", "rejection_reason": reason, "content_length": len(plan.get("content", ""))})
    write_json(OUTPUT / "rejected_plan_samples.json", rejected)

    sds = sds_pilot(sds_rows)
    write_json(OUTPUT / "sds_pilot_10.json", sds)

    category_candidates = Counter(item["category"] for item in groups["enterprise_special_plan"])
    retained = len(plans) - len(rejected)
    report = [
        "# 第二轮预案样本复核报告",
        "",
        "## 范围与边界",
        "",
        "本轮仅离线读取 `cleaned/selected_plan_samples.json`（43 条）及 `cleaned/sds_merged.json`；未联网、未继续爬取、未导入知识库、未生成最终模板，也未修改原始文件。",
        "",
        "## 分类结果",
        "",
        f"- 企业级专项应急预案：{len(groups['enterprise_special_plan'])} 条",
        f"- 政府/区域预案参考：{len(groups['government_or_regional_plan'])} 条",
        f"- 现场处置方案来源：{len(groups['onsite_disposal_plan'])} 条",
        f"- 拒绝：{len(rejected)} 条",
        f"- 已保留并按标题层级重切章节：{retained} 条",
        "",
        "## 企业专项预案候选量与目标差距",
        "",
        f"- 危化品泄漏：{category_candidates['chemical_leakage']} 条（目标 8–12，缺口 {max(0, 8-category_candidates['chemical_leakage'])}）",
        f"- 中毒窒息：{category_candidates['poisoning_asphyxia']} 条（目标 6–10，缺口 {max(0, 6-category_candidates['poisoning_asphyxia'])}）",
        f"- 有限空间：{category_candidates['confined_space']} 条（目标 6–10，缺口 {max(0, 6-category_candidates['confined_space'])}）",
        "",
        "现有 43 条中，绝大多数是区/镇/园区预案或现场处置方案；为避免将区域响应机制误当企业模板，本轮没有为了凑数量改变分级。",
        "",
        "## 重提取与脱敏规则",
        "",
        "- `sections` 依据正文中的章节/数字标题切分，标题和其后正文保存在同一对象中；目录引导符和仅有标题的碎片不入库。",
        "- 不使用一轮的 `accident_risks`、`handling_steps`、`ppe`、`first_aid`、`forbidden_actions` 字段。",
        "- 企业模板候选副本已对手机号、座机号、联系人字段、地址字段和邮箱做规则脱敏；原始文件未改动。",
        "- 两条‘窒息事故现场处置要点’中出现‘小动物试验’表述，保留为来源审查记录，但不得作为推荐处置措施复用。",
        "",
        "## 拒绝清单",
        "",
    ]
    for item in rejected:
        report.append(f"- {item['title']}：{item['rejection_reason']}")
    (OUTPUT / "plan_sample_review_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    status_counts = Counter(item["review_status"] for item in sds)
    problem_rows = [item["chemical_name"] for item in sds if item["conflict_notes"]]
    sds_report = [
        "# SDS 十种化学品试点复核报告",
        "",
        "## 范围",
        "",
        "仅处理甲醇、乙醇、苯、盐酸、硫酸、硝酸、氢氧化钠、氨、氯气、硫化氢；未请求网络资料，也未把 PubChem 或任一 SDS 全文写入知识条目。",
        "",
        "## 结果",
        "",
        f"- 输出化学品：{len(sds)} 种",
        f"- 审核状态：{dict(status_counts)}（均保持 `pending`，需专业人员逐项确认）",
        f"- 含来源冲突/缺口说明的项目：{', '.join(problem_rows)}",
        "",
        "## 重要审查结论",
        "",
        "- 苯：现有合并文件没有可用的苯条目，字段保持空白；没有用氯苯、甲苯等近似名称替代，也没有填写 CAS。",
        "- 乙醇：现有记录仅为 PubChem 聚合信息，CAS 保持空白，未据常识补填。",
        "- 甲醇：当前选中内容是含甲醇和苯酚的混合物，不能作为纯甲醇最终 SDS。",
        "- 盐酸：现有选中内容混入与盐酸不一致的乙酸乙酯信息，因此安全字段留空，等待逐页复核。",
        "- 每个非空知识条目均在文本内标出其来源 URL，`source_urls` 同时保留原始来源列表。",
        "",
        "## 使用限制",
        "",
        "本文件是审核样例，不是可直接执行的现场处置指令。尤其急救、泄漏处置和个人防护应在产品浓度、包装形态、现场风向与企业程序确认后，由具备资质的安全专业人员复核。",
    ]
    (OUTPUT / "sds_review_report.md").write_text("\n".join(sds_report) + "\n", encoding="utf-8")
    print(json.dumps({"plans": len(plans), "groups": {k: len(v) for k, v in groups.items()}, "rejected": len(rejected), "sds_pilot": len(sds)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
