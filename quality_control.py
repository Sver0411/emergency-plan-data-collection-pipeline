"""现有应急资料的离线清洗、去重、优选和 SDS 合并。

本脚本只读取本地 records/raw/json 文件，不访问互联网、不启动爬虫，也不覆盖原始
资料和现有知识库输出。清洗结果写入 cleaned/，供人工审核后再决定是否导入。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
RECORDS_PATH = ROOT / "json" / "records.json"
CATALOG_PATH = ROOT / "json" / "chemical_catalog.json"
CLEANED_DIR = ROOT / "cleaned"
REPORT_DIR = ROOT / "reports"

MIN_LENGTH = {
    "chemical_leakage": 1200,
    "poisoning_asphyxia": 1200,
    "confined_space": 1200,
    "other_accident": 900,
    "SDS": 1400,
    "accident_case": 450,
    "regulation": 700,
    "knowledge": 700,
    # 目录条目天然是短结构化记录，不按预案正文长度淘汰。
    "chemical_catalog": 100,
}

# 仅作为无标签页面的兜底识别词；有“产品名称/化学品中文名”字段时优先使用字段原文。
SDS_COMMON_HINTS = {
    "盐酸", "硫酸", "硝酸", "氢氟酸", "磷酸", "醋酸", "氢氧化钠", "氢氧化钾", "氨", "氨水", "氨气",
    "氯气", "氯化氢", "硫化氢", "一氧化碳", "二氧化硫", "二氧化氮", "光气", "甲醛", "苯", "甲苯", "二甲苯",
    "甲醇", "乙醇", "丙酮", "乙酸乙酯", "苯乙烯", "氯乙烯", "苯酚", "次氯酸钠", "过氧化氢", "丙烷", "丁烷",
    "电石", "硝酸铵", "过氧乙酸", "环氧乙烷", "丙烯腈", "丙烯", "乙烯", "乙炔", "氢气", "氧气", "二氧化碳",
    "一氧化氮", "二氧化氯", "氯甲烷", "二氯甲烷", "三氯甲烷", "四氯化碳", "三氯乙烯", "四氯乙烯", "氯苯", "溴甲烷",
    "氟化氢", "溴素", "硫磺", "三氧化硫", "硫化钠", "硫酸二甲酯", "氰化钠", "氰化钾", "氰化氢", "砷化氢", "磷化氢",
    "黄磷", "红磷", "五氧化二磷", "氯酸钾", "高锰酸钾", "重铬酸钾", "漂白粉", "次氯酸钙", "氯酸钠", "亚硝酸钠",
    "硝酸钾", "硝酸钠", "硝酸银", "过硫酸铵", "过硫酸钠", "过氧化钠", "过氧化钾", "金属钠", "金属钾", "镁粉",
    "铝粉", "锌粉", "碳化钙", "硅烷", "硼烷", "氯乙酸", "丙烯酸", "甲基丙烯酸", "丙烯酸甲酯", "丙烯酸乙酯",
    "甲苯二异氰酸酯", "异氰酸甲酯", "环氧氯丙烷", "乙二醇", "二甘醇", "正己烷", "正庚烷", "正辛烷", "环己烷", "环己酮",
    "乙酸乙烯酯", "乙酸酐", "甲酸", "草酸", "柠檬酸", "氯仿", "二硫化碳", "硝基苯", "苯胺", "乙醚", "四氢呋喃",
    "二甲基甲酰胺", "二甲基亚砜", "吡啶", "喹啉", "萘", "蒽",
}


def load_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    text = re.sub(r"\s+", "", text).lower()
    # 去掉 PDF 页码和常见目录引导点，降低同一文件不同解析版本的差异。
    text = re.sub(r"[.·…_\-]{3,}", "", text)
    text = re.sub(r"(?<!\d)\d{1,4}(?=页|$)", "", text)
    return text


def compact_cjk_spaces(text: str) -> str:
    """修复 PDF 字符级抽取把每个汉字拆开的问题，不改变英文和数字间空格。"""
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text or "")


def content_hash(text: str) -> str:
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()


def source_host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_official_host(host: str) -> bool:
    return host.endswith((".gov.cn", ".edu.cn", ".org.cn", ".gov", ".edu")) or host in {
        "www.mem.gov.cn", "www.mee.gov.cn", "www.samr.gov.cn", "openstd.samr.gov.cn",
        "www.npc.gov.cn", "pubchem.ncbi.nlm.nih.gov",
    }


def source_is_known(record: dict) -> tuple[bool, str]:
    url = str(record.get("url", ""))
    parsed = urlparse(url)
    source = str(record.get("source", "")).strip()
    if url.startswith("local://") and source:
        return True, ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "source_unknown"
    if not source or source.lower() in {"unknown", "来源不明", "未命名公开资料"}:
        return False, "source_unknown"
    # 直接 IP 的附件缺少可核验的站点身份，暂不进入整理结果。
    if is_ip_host(parsed.hostname):
        return False, "source_ip_unverified"
    return True, ""


def looks_garbled(text: str) -> tuple[bool, str]:
    if not text:
        return True, "content_empty"
    replacement_count = text.count("\ufffd")
    if replacement_count or re.search(r"(?:锟斤拷|Ã|Â|æ|å|ç){2,}", text):
        return True, "garbled_text"
    control_count = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    if control_count > max(3, len(text) // 200):
        return True, "control_characters"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 40:
        duplicate_ratio = 1 - len(set(lines)) / len(lines)
        if duplicate_ratio > 0.72 and len(set(lines)) < 30:
            return True, "repeated_garbled_text"
    return False, ""


def file_is_readable(record: dict) -> tuple[bool, str]:
    file_name = str(record.get("file", ""))
    if not file_name:
        return False, "file_missing"
    path = Path(file_name)
    if not path.exists():
        return False, "file_missing"
    try:
        if path.stat().st_size < 100:
            return False, "file_empty"
    except OSError:
        return False, "file_unreadable"
    return True, ""


def minimum_length_reason(record: dict) -> str:
    category = str(record.get("category", "knowledge"))
    required = MIN_LENGTH.get(category, 700)
    if len(str(record.get("content", ""))) < required:
        return f"content_too_short<{required}"
    return ""


def sentence_list(text: str) -> list[str]:
    pieces = re.split(r"[。；;！？!?\n]+", text or "")
    result: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        value = re.sub(r"\s+", " ", piece).strip(" ：:|·-")
        if len(value) < 5 or value in seen:
            continue
        seen.add(value)
        result.append(value[:600])
    return result


def select_sentences(text: str, patterns: tuple[str, ...], limit: int = 18) -> list[str]:
    result: list[str] = []
    for sentence in sentence_list(text):
        if any(re.search(pattern, sentence, re.I) for pattern in patterns):
            result.append(sentence)
            if len(result) >= limit:
                break
    return result


def structured_fields(text: str) -> dict[str, list[str]]:
    """从原文抽取字段，只保留原文句子，不生成新的处置结论。"""
    return {
        "accident_risks": select_sentences(text, (
            r"风险", r"危险性", r"危害", r"易燃", r"爆炸", r"腐蚀", r"毒", r"窒息", r"缺氧",
        )),
        "handling_steps": select_sentences(text, (
            r"泄漏", r"泄露", r"切断", r"隔离", r"疏散", r"通风", r"检测", r"收集", r"吸收",
            r"围堤", r"转移", r"洗消", r"现场处置", r"应急处置",
        )),
        "ppe": select_sentences(text, (
            r"防护", r"呼吸器", r"防毒面具", r"防护服", r"防酸碱", r"手套", r"护目镜", r"安全带",
        )),
        "first_aid": select_sentences(text, (
            r"急救", r"吸入", r"皮肤接触", r"眼睛接触", r"食入", r"冲洗", r"就医", r"人工呼吸",
            r"心肺复苏",
        )),
        "forbidden_actions": select_sentences(text, (
            r"禁止", r"严禁", r"不得", r"切勿", r"避免", r"不应", r"不能",
        )),
    }


def plan_relevance(record: dict, category: str) -> tuple[bool, str]:
    """校正拆分汇编造成的误分类，只保留真正对应事故主题的预案。"""
    if record.get("type") != "emergency_plan":
        return False, "not_emergency_plan"
    title = compact_cjk_spaces(str(record.get("title", "")))
    content = compact_cjk_spaces(str(record.get("content", "")))
    headline = f"{title}\n{content[:4500]}"
    # 汇编正文中的泛化章节可能偶然出现“泄漏/窒息”字样；标题或首段必须明确是预案/处置方案。
    if not re.search(r"应急预案|现场处置方案|现场处置要点|处置方案", title):
        return False, "plan_title_not_explicit"
    if len(title) > 90 or re.search(r"负责组织|组织制定|组织各|每年至少|演练形式|适用范围\s*[:：]", title):
        return False, "title_fragment"
    if category == "chemical_leakage":
        if not re.search(r"危险化学品|危化品|化学品|化工|泄漏|泄露|液氨|氯气|甲醇|储罐|罐区|燃气|管道", title):
            return False, "title_topic_mismatch"
        chemical = re.search(r"危险化学品|危化品|化学品|化工|液氨|氯气|甲醇|储罐|罐区|有毒气体", headline)
        leakage = len(re.findall(r"泄漏|泄露|跑冒滴漏", headline))
        first_block = content[:900]
        title_chemical = re.search(r"危险化学品|危化品|化学品|化工|液氨|氯气|甲醇|储罐|罐区|泄漏|泄露", title)
        first_block_chemical = re.search(r"危险化学品|危化品|化学品|化工|液氨|氯气|甲醇|储罐|罐区|有毒气体", first_block)
        first_block_leakage = re.search(r"泄漏|泄露|跑冒滴漏", first_block)
        if not chemical or (not title_chemical and not (first_block_chemical and first_block_leakage)) or (leakage < 2 and "泄漏" not in title and "泄露" not in title):
            return False, "topic_mismatch"
    elif category == "poisoning_asphyxia":
        if not re.search(r"中毒|窒息|硫化氢|一氧化碳|有毒气体|缺氧|职业病|瓦斯", title):
            return False, "title_topic_mismatch"
        signals = len(re.findall(r"中毒|窒息|硫化氢|一氧化碳|有毒气体|缺氧", headline))
        title_poison = re.search(r"中毒|窒息|硫化氢|一氧化碳|有毒气体|缺氧", title)
        first_block_poison = re.search(r"中毒|窒息|硫化氢|一氧化碳|有毒气体|缺氧", content[:900])
        if signals < 2 or (not title_poison and not first_block_poison):
            return False, "topic_mismatch"
    elif category == "confined_space":
        if not re.search(r"有限空间|受限空间|密闭空间|罐内|化粪池|污水井", title):
            return False, "title_topic_mismatch"
        signals = len(re.findall(r"有限空间|受限空间|密闭空间|罐内|化粪池|污水井", headline))
        title_space = re.search(r"有限空间|受限空间|密闭空间|罐内|化粪池|污水井", title)
        first_block_space = re.search(r"有限空间|受限空间|密闭空间|罐内|化粪池|污水井", content[:900])
        if signals < 2 or (not title_space and not first_block_space):
            return False, "topic_mismatch"
    fields = structured_fields(content)
    if len(fields["handling_steps"]) < 2:
        return False, "missing_handling_sections"
    if len(fields["accident_risks"]) < 1:
        return False, "missing_risk_analysis"
    return True, ""


def record_quality_score(record: dict) -> int:
    content = str(record.get("content", ""))
    category = str(record.get("category", ""))
    host = source_host(str(record.get("url", "")))
    fields = structured_fields(content)
    score = min(int(record.get("quality_score", 0) or 0), 70)
    if is_official_host(host):
        score += 15
    elif host:
        score += 5
    score += min(len(content) // 5000, 8)
    score += min(len(fields["handling_steps"]), 5) * 2
    score += min(len(fields["ppe"]), 3)
    score += min(len(fields["first_aid"]), 3)
    score += min(len(fields["forbidden_actions"]), 2)
    if category in {"chemical_leakage", "poisoning_asphyxia", "confined_space"}:
        score += 6 if record.get("type") == "emergency_plan" else 0
    return min(score, 100)


def choose_better(left: dict, right: dict) -> dict:
    left_key = (int(left.get("_selection_score", 0)), len(str(left.get("content", ""))), int(left.get("quality_score", 0)))
    right_key = (int(right.get("_selection_score", 0)), len(str(right.get("content", ""))), int(right.get("quality_score", 0)))
    return left if left_key >= right_key else right


def high_similarity(left: dict, right: dict) -> float:
    a = normalized_text(str(left.get("content", "")))[:16000]
    b = normalized_text(str(right.get("content", "")))[:16000]
    if not a or not b:
        return 0.0
    # 长 PDF 逐字符 SequenceMatcher 的复杂度很高；固定长度重叠片段的 Jaccard
    # 指纹足以识别同一附件的重复解析版本，也不会阻塞整批离线清洗。
    window, stride = 80, 40
    left_shingles = {a[index:index + window] for index in range(0, max(1, len(a) - window), stride)}
    right_shingles = {b[index:index + window] for index in range(0, max(1, len(b) - window), stride)}
    union = left_shingles | right_shingles
    if not union:
        return 1.0 if a == b else 0.0
    return len(left_shingles & right_shingles) / len(union)


def deduplicate(records: list[dict], similarity_threshold: float = 0.94) -> tuple[list[dict], list[dict]]:
    """先去正文完全重复，再在同类资料中去高度重复版本。"""
    exact: dict[str, dict] = {}
    duplicate_log: list[dict] = []
    for record in records:
        digest = content_hash(str(record.get("content", "")))
        current = exact.get(digest)
        if current is None:
            exact[digest] = record
            continue
        chosen = choose_better(current, record)
        removed = record if chosen is current else current
        exact[digest] = chosen
        duplicate_log.append({
            "kind": "exact_duplicate",
            "kept_title": chosen.get("title", ""),
            "kept_url": chosen.get("url", ""),
            "removed_title": removed.get("title", ""),
            "removed_url": removed.get("url", ""),
            "similarity": 1.0,
        })

    by_category: dict[str, list[dict]] = defaultdict(list)
    for record in exact.values():
        by_category[str(record.get("category", ""))].append(record)
    kept: list[dict] = []
    # 目录条目以 CAS/品名为结构化字段，不做正文相似度淘汰。
    for category, items in by_category.items():
        if category == "chemical_catalog":
            kept.extend(items)
            continue
        ordered = sorted(items, key=lambda item: (
            int(item.get("_selection_score", 0)), len(str(item.get("content", ""))), int(item.get("quality_score", 0))
        ), reverse=True)
        category_kept: list[dict] = []
        for candidate in ordered:
            duplicate_of = None
            best_similarity = 0.0
            for previous in category_kept:
                similarity = high_similarity(candidate, previous)
                if similarity > best_similarity:
                    best_similarity = similarity
                if similarity >= similarity_threshold:
                    duplicate_of = previous
                    break
            if duplicate_of is None:
                category_kept.append(candidate)
            else:
                duplicate_log.append({
                    "kind": "high_similarity",
                    "kept_title": duplicate_of.get("title", ""),
                    "kept_url": duplicate_of.get("url", ""),
                    "removed_title": candidate.get("title", ""),
                    "removed_url": candidate.get("url", ""),
                    "similarity": round(best_similarity, 4),
                })
        kept.extend(category_kept)
    return kept, duplicate_log


def catalog_name_maps() -> tuple[list[str], dict[str, str], dict[str, set[str]], dict[str, set[str]]]:
    catalog = load_json(CATALOG_PATH, [])
    names: set[str] = {
        "盐酸", "硫酸", "硝酸", "氢氟酸", "磷酸", "醋酸", "氢氧化钠", "氢氧化钾", "氨", "氨水",
        "氨气", "氯气", "氯化氢", "硫化氢", "一氧化碳", "二氧化硫", "二氧化氮", "甲醇", "乙醇",
        "苯", "甲苯", "二甲苯", "次氯酸钠", "过氧化氢", "氯乙烯", "环氧乙烷",
    }
    aliases: dict[str, str] = {}
    cas_by_name: dict[str, set[str]] = defaultdict(set)
    names_by_cas: dict[str, set[str]] = defaultdict(set)
    if isinstance(catalog, list):
        for entry in catalog:
            chemical = str(entry.get("chemical", "")).strip()
            if not chemical:
                continue
            names.add(chemical)
            aliases[chemical] = chemical
            cas = str(entry.get("cas", "")).strip()
            if cas:
                cas_by_name[chemical].add(cas)
                names_by_cas[cas].add(chemical)
            for alias in re.split(r"[；;]", str(entry.get("alias", ""))):
                alias = alias.strip()
                if len(alias) >= 2:
                    names.add(alias)
                    aliases[alias] = chemical
                    if cas:
                        cas_by_name[alias].add(cas)
                        names_by_cas[cas].add(chemical)
    names = {name for name in names if len(name) >= 2}
    return sorted(names, key=len, reverse=True), aliases, cas_by_name, names_by_cas


def extract_cas(text: str, chemical: str = "") -> str:
    text = text or ""
    explicit = re.findall(
        r"(?:CAS(?:号| No\.?| Number)?|化学文摘登记号)[^0-9]{0,24}(\d{2,7}-\d{2}-\d)(?!\d)",
        text[:8000], re.I,
    )
    candidates = explicit + re.findall(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)", text[:8000])
    candidates = list(dict.fromkeys(candidates))
    valid = []
    for candidate in candidates:
        # 排除常见日期形式，保留 CAS 三段数字格式。
        if re.match(r"(?:19|20)\d{2}-", candidate):
            continue
        valid.append(candidate)
    if not valid:
        return ""
    if explicit:
        return next((candidate for candidate in explicit if candidate in valid), valid[0])
    if chemical:
        positions = [match.start() for match in re.finditer(re.escape(chemical), text[:8000])]
        if positions:
            return min(valid, key=lambda candidate: min(
                abs(text.find(candidate) - position) for position in positions if text.find(candidate) >= 0
            ))
    return valid[0]


def canonical_from_label(label: str, names: list[str], aliases: dict[str, str]) -> str:
    label = re.sub(r"(?<!\d)\d{2,7}-\d{2}-\d(?!\d)", "", label)
    label = re.sub(r"(?:产品名称|产品说明|化学品名称|化學品名稱)\s*", "", label)
    label = re.sub(r"^[\s/\\|:：]+", "", label)
    label = re.sub(r"\s+", " ", label).strip(" ：:|;")
    if not label or re.fullmatch(r"(?:产品编号|编号|推荐用途|化学品英文名称|产品代码|无|不适用)", label):
        return ""
    matches = [name for name in names if name in label and name not in {"化学品", "物质"}]
    if matches:
        return aliases.get(max(matches, key=len), max(matches, key=len))
    return label[:80]


def extract_chemical(
    record: dict,
    names: list[str],
    aliases: dict[str, str],
    cas_by_name: dict[str, set[str]],
    names_by_cas: dict[str, set[str]],
    name_pattern: re.Pattern[str],
) -> tuple[str, str]:
    """产品名称优先，CAS 只在产品字段附近提取并用目录 CAS 做一致性校验。"""
    title = compact_cjk_spaces(str(record.get("title", "")))
    content = compact_cjk_spaces(str(record.get("content", "")))
    text = "\n".join((title, content[:8000], str(record.get("file", ""))))
    labels: list[str] = []
    pubchem_title = re.search(r"^(.+?)\s+PubChem安全数据", title)
    if pubchem_title:
        labels.append(pubchem_title.group(1))
    labels.extend(re.findall(
        r"(?:产品名称(?:\s*/\s*推荐用途)?|产品说明|化学品中文(?:名|名称)|化學品名稱|物质名称|Product name)\s*[:：]?\s*([^\n|；;]{2,120})",
        text[:8000],
    ))
    best_name = ""
    for label in labels:
        candidate = canonical_from_label(label, names, aliases)
        if candidate and len(candidate) >= 2:
            best_name = candidate
            break
    if not best_name:
        for match in name_pattern.finditer("\n".join((title, content[:2500], str(record.get("file", ""))))):
            candidate = aliases.get(match.group(0), match.group(0))
            if len(candidate) >= 2:
                best_name = candidate
                break
    cas = extract_cas(text, best_name)
    if best_name and re.search(r"推荐用途|企业名称|供应商|SDS编号|化学品英文名称", best_name):
        best_name = ""
    if not best_name and cas and len(names_by_cas.get(cas, set())) == 1:
        best_name = next(iter(names_by_cas[cas]))
    known_cas = cas_by_name.get(best_name, set())
    # 目录只有一个 CAS 时，修正 PDF 将其它章节 CAS 误放在产品信息前面的情况。
    if best_name and len(known_cas) == 1:
        only_cas = next(iter(known_cas))
        if not cas or cas not in known_cas:
            cas = only_cas
    return best_name, cas

def sds_completeness(record: dict, fields: dict[str, list[str]]) -> int:
    content = str(record.get("content", ""))
    host = source_host(str(record.get("url", "")))
    section_hits = sum(1 for pattern in (
        r"化学品及企业标识|产品标识", r"危险性概述|危险性类别", r"急救措施|急救", r"消防措施",
        r"泄漏应急处理|意外释放", r"接触控制|个体防护", r"废弃处置", r"运输信息", r"法规信息",
    ) if re.search(pattern, content))
    score = min(len(content) // 1000, 35) + section_hits * 5
    score += min(len(fields["handling_steps"]), 8) * 2
    score += min(len(fields["ppe"]), 5) * 2
    score += min(len(fields["first_aid"]), 5) * 2
    score += min(len(fields["forbidden_actions"]), 3)
    if host == "pubchem.ncbi.nlm.nih.gov":
        score += 8
    elif host and not is_ip_host(host):
        score += 12
    if re.search(r"修订日期|版本|SDS编号|CAS", content, re.I):
        score += 8
    return score


def merge_sds(
    records: list[dict],
    names: list[str],
    aliases: dict[str, str],
    cas_by_name: dict[str, set[str]],
    names_by_cas: dict[str, set[str]],
    name_pattern: re.Pattern[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    unresolved: list[dict] = []
    for record in records:
        chemical, cas = extract_chemical(record, names, aliases, cas_by_name, names_by_cas, name_pattern)
        if not chemical and not cas:
            unresolved.append({
                "title": record.get("title", ""), "url": record.get("url", ""),
                "reason": "chemical_name_and_cas_unresolved",
            })
            continue
        fields = structured_fields(str(record.get("content", "")))
        candidate = dict(record)
        candidate["chemical"] = chemical
        candidate["cas"] = cas
        candidate["_selection_score"] = sds_completeness(record, fields)
        candidate["_fields"] = fields
        # CAS 作为强标识；没有 CAS 时按规范化中文名合并。
        key = (chemical, cas) if cas else (chemical, "")
        groups[key].append(candidate)

    merged: list[dict] = []
    merge_log: list[dict] = []
    for key, candidates in sorted(groups.items()):
        selected = max(candidates, key=lambda item: (
            int(item.get("_selection_score", 0)), len(str(item.get("content", ""))), int(item.get("quality_score", 0))
        ))
        fields = selected.pop("_fields")
        selection_score = selected.pop("_selection_score")
        merged.append({
            "chemical": key[0],
            "cas": key[1],
            "title": selected.get("title", ""),
            "source": selected.get("source", ""),
            "source_url": selected.get("url", ""),
            "source_urls": sorted({str(item.get("url", "")) for item in candidates}),
            "source_file": selected.get("file", ""),
            "selection_score": selection_score,
            "quality_score": selected.get("quality_score", 0),
            "content": selected.get("content", ""),
            "accident_risks": fields["accident_risks"],
            "handling_steps": fields["handling_steps"],
            "ppe": fields["ppe"],
            "first_aid": fields["first_aid"],
            "forbidden_actions": fields["forbidden_actions"],
            "merged_record_count": len(candidates),
        })
        if len(candidates) > 1:
            merge_log.append({
                "chemical": key[0], "cas": key[1], "kept_url": selected.get("url", ""),
                "input_count": len(candidates), "discarded_urls": sorted({str(item.get("url", "")) for item in candidates if item is not selected}),
            })
    return merged, unresolved, merge_log


def minimal_record(record: dict) -> dict:
    return {
        "title": record.get("title", ""), "category": record.get("category", ""),
        "url": record.get("url", ""), "file": record.get("file", ""),
        "content_length": len(str(record.get("content", ""))),
    }


def main() -> None:
    records = load_json(RECORDS_PATH, [])
    if not isinstance(records, list):
        raise RuntimeError(f"records.json 不是数组：{RECORDS_PATH}")
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    excluded: list[dict] = []
    valid: list[dict] = []
    reason_counts: Counter[str] = Counter()
    for original in records:
        record = dict(original)
        category = str(record.get("category", "knowledge"))
        reasons: list[str] = []
        known, source_reason = source_is_known(record)
        if not known:
            reasons.append(source_reason)
        readable, file_reason = file_is_readable(record)
        if not readable:
            reasons.append(file_reason)
        garbled, garble_reason = looks_garbled(str(record.get("content", "")))
        if garbled:
            reasons.append(garble_reason)
        short_reason = minimum_length_reason(record)
        if short_reason:
            reasons.append(short_reason)
        if category in {"chemical_leakage", "poisoning_asphyxia", "confined_space"}:
            relevant, topic_reason = plan_relevance(record, category)
            if not relevant:
                reasons.append(topic_reason)
        if reasons:
            excluded.append({**minimal_record(record), "reasons": list(dict.fromkeys(reasons))})
            reason_counts.update(dict.fromkeys(reasons, 1))
            continue
        record["_selection_score"] = record_quality_score(record)
        valid.append(record)

    deduped, duplicate_log = deduplicate(valid)
    deduped_by_category = Counter(str(record.get("category", "")) for record in deduped)
    duplicate_removed = len(valid) - len(deduped)

    # SDS 先从去重后的记录中按化学品名称/CAS 合并，未识别者只进入排除报告。
    sds_records = [record for record in deduped if record.get("category") == "SDS"]
    non_sds_records = [record for record in deduped if record.get("category") != "SDS"]
    names, aliases, cas_by_name, names_by_cas = catalog_name_maps()
    # 仅编译常见物质兜底词；完整目录名通过产品名称字段匹配，避免巨大重叠正则。
    pattern_names = sorted({name for name in names if name in SDS_COMMON_HINTS}, key=len, reverse=True)
    name_pattern = re.compile("|".join(re.escape(name) for name in pattern_names))
    merged_sds, unresolved_sds, sds_merge_log = merge_sds(sds_records, names, aliases, cas_by_name, names_by_cas, name_pattern)
    for item in unresolved_sds:
        item["category"] = "SDS"
        item["reasons"] = [item.pop("reason")]
        excluded.append(item)
        reason_counts.update(item["reasons"])

    # 清理内部排序字段，输出不暴露实现细节。
    for record in non_sds_records:
        record.pop("_selection_score", None)
    curated_records = non_sds_records + [
        {
            "title": item["title"], "source": item["source"], "url": item["source_url"],
            "type": "knowledge", "category": "SDS", "risk_category": [], "industry": "chemical",
            "content": item["content"], "keywords": [item["chemical"], item["cas"]] if item["cas"] else [item["chemical"]],
            "chemical": item["chemical"], "cas": item["cas"],
        }
        for item in merged_sds
    ]

    # 面向人工验收的高质量预案清单，按类别输出结构化字段。
    selected_plans: list[dict] = []
    for record in non_sds_records:
        category = str(record.get("category", ""))
        if category not in {"chemical_leakage", "poisoning_asphyxia", "confined_space"}:
            continue
        fields = structured_fields(str(record.get("content", "")))
        selected_plans.append({
            "title": record.get("title", ""), "category": category, "industry": record.get("industry", ""),
            "source": record.get("source", ""), "source_url": record.get("url", ""),
            "source_file": record.get("file", ""), "quality_score": record.get("quality_score", 0),
            "selection_score": record_quality_score(record), "content": record.get("content", ""),
            **fields,
        })
    selected_plans.sort(key=lambda item: (item["category"], -int(item["selection_score"]), -len(item["content"])))

    # 综合整理结果只包含高质量预案和每种化学品选中的 SDS，不会覆盖 json/knowledge.json。
    structured_materials = selected_plans + [
        {
            "title": item["title"], "category": "SDS", "chemical": item["chemical"], "cas": item["cas"],
            "source": item["source"], "source_url": item["source_url"], "source_urls": item["source_urls"],
            "quality_score": item["quality_score"], "selection_score": item["selection_score"],
            "content": item["content"], "accident_risks": item["accident_risks"],
            "handling_steps": item["handling_steps"], "ppe": item["ppe"],
            "first_aid": item["first_aid"], "forbidden_actions": item["forbidden_actions"],
        }
        for item in merged_sds
    ]

    write_json(CLEANED_DIR / "curated_records.json", curated_records)
    write_json(CLEANED_DIR / "selected_plan_samples.json", selected_plans)
    write_json(CLEANED_DIR / "sds_merged.json", merged_sds)
    write_json(CLEANED_DIR / "structured_materials.json", structured_materials)
    write_json(CLEANED_DIR / "excluded_records.json", excluded)
    write_json(REPORT_DIR / "duplicate_groups.json", duplicate_log)
    write_json(REPORT_DIR / "sds_merge_log.json", sds_merge_log)

    report = {
        "mode": "offline_quality_control_only",
        "network_access": False,
        "input_records": len(records),
        "valid_before_dedup": len(valid),
        "curated_records": len(curated_records),
        "excluded_records": len(excluded),
        "excluded_reason_counts": dict(reason_counts),
        "exact_or_high_similarity_removed": duplicate_removed,
        "duplicate_log_count": len(duplicate_log),
        "input_category_counts": dict(Counter(str(record.get("category", "")) for record in records)),
        "deduped_category_counts": dict(deduped_by_category),
        "selected_plan_counts": dict(Counter(item["category"] for item in selected_plans)),
        "sds_input_records": len(sds_records),
        "sds_merged_chemical_groups": len(merged_sds),
        "sds_unresolved_records": len(unresolved_sds),
        "sds_groups_with_multiple_versions": sum(1 for item in sds_merge_log if item["input_count"] > 1),
        "source_urls_in_curated_records": len({str(record.get("url", "")) for record in curated_records}),
        "output_root": str(CLEANED_DIR),
        "knowledge_base_imported": False,
        "policy": {
            "raw_archive_preserved": True,
            "content_similarity_threshold": 0.94,
            "minimum_lengths": MIN_LENGTH,
            "sds_selection": "按化学品名称/CAS分组，按章节覆盖、处置字段、PPE、急救、版本/修订信息和来源可核验性择优",
        },
    }
    write_json(REPORT_DIR / "quality_control_report.json", report)

    md = [
        "# 应急资料离线清洗报告",
        "",
        "本报告由本地已有 records.json 生成；本次没有访问网络，也没有导入知识库。",
        "",
        "## 结果概览",
        "",
        f"- 输入记录：{len(records)}",
        f"- 清洗后统一记录：{len(curated_records)}",
        f"- 排除记录：{len(excluded)}",
        f"- 完全重复/高度重复移除：{duplicate_removed}",
        f"- 高质量预案：{len(selected_plans)}",
        f"- SDS 合并后化学品组：{len(merged_sds)}",
        "",
        "## 高质量预案数量",
        "",
        "| 类别 | 数量 |",
        "|---|---:|",
    ]
    for category, count in sorted(Counter(item["category"] for item in selected_plans).items()):
        md.append(f"| {category} | {count} |")
    md += [
        "",
        "## SDS 合并规则",
        "",
        "按化学品名称和 CAS 号分组；同组保留章节覆盖更全、包含泄漏处置/PPE/急救/禁止事项且来源可核验的版本，其余 URL 写入合并日志。",
        "",
        "## 输出文件",
        "",
        "- `cleaned/selected_plan_samples.json`：高质量危化品泄漏、中毒窒息、有限空间预案。",
        "- `cleaned/sds_merged.json`：每种化学品择优后的 SDS 及结构化字段。",
        "- `cleaned/structured_materials.json`：预案和 SDS 的综合人工审核集。",
        "- `cleaned/curated_records.json`：清洗后的统一记录，尚未导入知识库。",
        "- `cleaned/excluded_records.json`：剔除原因和来源定位。",
        "- `reports/duplicate_groups.json`、`reports/sds_merge_log.json`：去重与合并审计日志。",
        "",
        "原始 raw 文件未物理删除，作为可追溯归档；清洗结果已从候选集和后续导入范围中剔除问题记录。",
    ]
    (REPORT_DIR / "quality_control_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
