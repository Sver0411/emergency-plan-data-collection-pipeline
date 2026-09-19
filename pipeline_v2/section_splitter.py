from __future__ import annotations
import re
from .models import PlanSection
from .normalization import CAS_RE, PHONE_RE

BAD_TOPICS = {"自然灾害", "防洪防汛", "电气火灾", "建筑物倒塌", "交通事故"}
HEADING_RE = re.compile(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)(?:\s+|(?=[\u4e00-\u9fff]))(.{2,55})$")
# Numbered duties, notices, references and complete action sentences are not
# section headings even when they happen to start with a chapter-like number.
LIST_VERB_RE = re.compile(r"^(?:负责|组织|做好|切断|立即|根据|进行|参与|发生|按照|开展|编制|发布|修订|执行|贯彻)")

def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 65 or CAS_RE.fullmatch(line) or PHONE_RE.fullmatch(line): return False
    if re.fullmatch(r"[\d.\s]+", line) or re.fullmatch(r"\d+[\s.]+\d+(?:[\s.]+\d+)+", line): return False
    if re.fullmatch(r"第?\d+页(?:/共\d+页)?", line) or re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", line): return False
    if re.match(r"^(?:总指挥|联系人|负责人|应急预案编号)\s*[:：]", line): return False
    if re.fullmatch(r"\d+号）?", line) or re.search(r"\d+小时(?:以上)?", line): return False
    if re.search(r"\d+(?:万|亿)?元(?:以上|以下|以内)?(?:直接经济损失)?", line): return False
    if "..." in line or "……" in line or line.count("，") >= 2: return False
    match = HEADING_RE.match(line)
    if not match: return False
    phrase = match.group(1).strip()
    if re.fullmatch(r"\d+号[）)]?", phrase) or re.fullmatch(r"\d+(?:\.\d+)?\s*[%％]", phrase): return False
    if re.match(r"^(?:总指挥|联系人|负责人|应急预案编号)\s*[:：]", phrase): return False
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", phrase))
    if LIST_VERB_RE.match(phrase) or re.match(r"^(?:已经|现印发|现发布|通知)", phrase): return False
    if phrase.endswith(("。", "；", ";", "，", ",")): return False
    if chinese_count > 35 and (LIST_VERB_RE.search(phrase) or re.search(r"[。；;]", phrase)): return False
    if re.search(r"(?:应当|必须|需要|由.+负责|并及时|确保).{5,}", phrase) and chinese_count > 20: return False
    return True

def is_toc_fragment(text: str) -> bool:
    """A table of contents is navigation only unless matching section bodies are present."""
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines: return False
    toc_lines=sum(bool(re.search(r"\.{3,}|…{2,}|\s\d{1,3}$", line)) for line in lines)
    if len(text) > 5000: return False
    explicit_toc = bool(re.search(r"(?:^|\n)\s*目\s*录\s*(?:\n|$)", text[:800]))
    toc_ratio = toc_lines / max(1, len(lines))
    return toc_lines >= 3 and toc_ratio >= 0.35 and (explicit_toc or toc_lines >= 5)

def split_sections(text: str, category: str = "other") -> list[PlanSection]:
    # 综合文件被采集为单一专项记录时，只保留目标专项开始后的连续片段；
    # 遇到另一专项的显式标题立即截断，防止有限空间记录串入火灾、坍塌等内容。
    if category == "confined_space":
        start_match = re.search(r"有限空间|受限空间", text)
        if start_match:
            tail = text[start_match.end():]
            foreign = re.search(r"(?m)^\s*(?:第?\d+(?:\.\d+)*[、.．]?\s*)?(?:危险化学品泄漏|火灾[、，]?爆炸|坍塌|自然灾害|防洪防汛|电气火灾|建筑物倒塌|交通事故)(?:专项)?(?:应急预案|现场处置方案|事故)?", tail)
            if foreign:
                text = text[:start_match.end()+foreign.start()]
    lines = text.splitlines(); starts=[]
    pos=0
    for line in lines:
        if _is_heading(line): starts.append((pos, line.strip()))
        pos += len(line)+1
    sections=[]
    for i,(start,heading) in enumerate(starts):
        end=starts[i+1][0] if i+1 < len(starts) else len(text)
        body=text[start+len(heading):end].strip()
        # 只要求标题后有可辨识正文；过高的字数门槛会漏掉短而完整的章节。
        if len(body) < 6: continue
        topic="general"
        warnings=[]
        if category == "confined_space" and any(x in heading for x in BAD_TOPICS):
            break
        sections.append(PlanSection(section_id=f"s{i+1:03d}",heading=heading,content=body,start_position=start,end_position=end,topic=topic,boundary_confidence=0.9,warnings=warnings))
    return sections
