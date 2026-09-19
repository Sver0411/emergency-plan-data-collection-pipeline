from __future__ import annotations

import re

from .models import PlanSection
from .normalization import CAS_RE, PHONE_RE


HEADING_RE = re.compile(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)(?:\s+|(?=[\u4e00-\u9fff]))(.{2,80})$")
SPECIAL_TITLE_RE = re.compile(r"^(?:《[^》]{4,100}》)?[^。；;]{2,100}(?:专项应急预案|专项预案|现场处置方案|综合应急预案)$")
BAD_START_RE = re.compile(r"^(?:负责|组织|采取|确认|立即|发生|按照|根据|参与|开展|编制|发布|修订|执行|结合|接到|当|若|如|等相关)")


def _is_heading_v24(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 120 or CAS_RE.fullmatch(line) or PHONE_RE.fullmatch(line):
        return False
    # Extracted personnel/table rows frequently begin with a number but also
    # contain a phone number, several tabular columns, or credential fields.
    # They are data rows, never chapter headings.
    if re.search(r"(?<!\d)1\d{10}(?!\d)|(?<!\d)0\d{2,3}[-－]\d{7,8}(?!\d)", line):
        return False
    if re.fullmatch(r"[\d.\s]+", line) or re.fullmatch(r"\d+[\s.]+\d+(?:[\s.]+\d+)+", line):
        return False
    if re.fullmatch(r"第?\d+页(?:/共\d+页)?", line) or re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", line):
        return False
    if re.search(r"\d+小时(?:以上|以下)?", line) or re.search(r"\d+(?:万|亿)?元(?:以上|以下|以内)?", line):
        return False
    if re.match(r"^(?:总指挥|联系人|负责人|应急预案编号)\s*[:：]", line):
        return False
    if re.search(r"(?:已经.{0,30}(?:同意|批准).{0,20}(?:印发|发布)|现印发|通知)", line):
        return False
    if "..." in line or "……" in line or line.count("，") >= 2:
        return False
    if SPECIAL_TITLE_RE.fullmatch(line):
        return not bool(BAD_START_RE.match(line))
    match = HEADING_RE.match(line)
    if not match:
        return False
    phrase = match.group(1).strip()
    if re.fullmatch(r"\d+号[）)]?", phrase) or re.fullmatch(r"\d+(?:\.\d+)?\s*[%％]", phrase):
        return False
    if re.match(r"^(?:总指挥|联系人|负责人|应急预案编号)\s*[:：]", phrase):
        return False
    if BAD_START_RE.match(phrase) or phrase.endswith(("。", "；", ";", "，", ",")):
        return False
    # A colon-led parameter/list line or a long action sentence is body text,
    # even when a PDF extractor put a numeric prefix in front of it.  This
    # catches risk descriptions, first-aid steps and table rows without
    # discarding short, ordinary subsection headings.
    if re.search(r"[：:]", phrase) and len(re.findall(r"[\u4e00-\u9fff]", phrase)) > 12:
        return False
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", phrase))
    if chinese_count > 28 and re.search(r"应立即|立即|进行|采取|支持生命|抢救|救治|导致|引发|汇报|报告|造成|可能|并", phrase):
        return False
    if chinese_count > 20 and len(re.findall(r"\s+", phrase)) >= 3:
        return False
    if chinese_count > 35 and re.search(r"[。；;]", phrase):
        return False
    if re.search(r"(?:应当|必须|需要|由.+负责|并及时|确保).{5,}", phrase) and len(re.findall(r"[\u4e00-\u9fff]", phrase)) > 20:
        return False
    return True


def split_sections_v24(text: str, category: str = "other") -> list[PlanSection]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    position = 0
    for line in lines:
        if _is_heading_v24(line):
            starts.append((position, line.strip()))
        position += len(line) + 1
    sections: list[PlanSection] = []
    for index, (start, heading) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        body = text[start + len(heading):end].strip()
        if len(body) < 6:
            continue
        sections.append(
            PlanSection(
                section_id=f"s{index + 1:03d}",
                heading=heading,
                content=body,
                start_position=start,
                end_position=end,
                topic=category or "general",
                boundary_confidence=0.9,
                warnings=[],
            )
        )
    return sections


def audit_heading(heading: str, content: str, category: str = "other") -> list[str]:
    """Independent audit used for review; it does not silently discard findings."""
    reasons: list[str] = []
    chinese = len(re.findall(r"[\u4e00-\u9fff]", heading))
    if chinese > 30:
        reasons.append("heading_over_30_chinese_chars")
    if BAD_START_RE.match(re.sub(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十百]+[、.]|[（(]?\d+(?:\.\d+){0,4}[）)、.．]?)\s*", "", heading)):
        reasons.append("sentence_like_heading")
    if re.fullmatch(r"[\d.\s]+|\d+小时(?:以上|以下)?|\d+(?:万|亿)?元(?:以上|以下|以内)?", heading):
        reasons.append("time_amount_or_number_heading")
    body_probe = content[:300]
    title_topics = set(re.findall(r"有限空间|受限空间|泄漏|火灾|爆炸|中毒|窒息|防汛|水灾|重大危险源", heading))
    body_topics = set(re.findall(r"有限空间|受限空间|泄漏|火灾|爆炸|中毒|窒息|防汛|水灾|重大危险源", body_probe))
    # A normal numbered subsection may legitimately describe a different risk
    # facet than its parent heading.  Apply title/body topic mismatch only to a
    # standalone special-plan heading, where a cross-special split is material.
    if SPECIAL_TITLE_RE.fullmatch(heading) and title_topics and body_topics and not (title_topics & body_topics):
        reasons.append("title_body_mismatch")
    return reasons
