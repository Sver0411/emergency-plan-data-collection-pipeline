"""V2.4.2.1 local-segment classification hotfix.

Parent classification is out of scope. Every decision in this module is based
on the current record's own local text before optional parent context.
"""
from __future__ import annotations

import re


TOC_LINE_RE = re.compile(r"^.{2,120}(?:\.{4,}|…{3,}|-{5,})\s*\d{1,4}\s*$")
ONSITE_TITLE_RE = re.compile(r"现场处置方案|现场处置要点|应急处置要点")
ACTION_RE = re.compile(r"首先|其次|立即|疏散|警戒|扑救|堵漏|救援|切断|冷却|转移|报警|防护|灭火")
BODY_MARKER_RE = re.compile(r"应采取以下措施|风险分析|危险性分析|事故风险|处置措施|组织机构|应急响应|应急结束|应急保障")
FORMAL_HEADING_RE = re.compile(
    r"(?m)^\s*(?:第?[一二三四五六七八九十百]+[章节、.]|\d+(?:\.\d+){0,3}[、.．]?)\s*"
    r"(?:编制目的|适用范围|风险分析|危险性分析|组织机构|应急组织|预防措施|应急响应|处置方案|处置措施|应急保障|应急结束|附则)"
)

STRUCTURE_PATTERNS = {
    "purpose": r"编制目的",
    "scope": r"适用范围",
    "risk": r"事故风险|风险分析|危险性分析|危险有害因素",
    "organization": r"组织机构(?:和|及)?职责|应急组织|指挥部.{0,20}职责",
    "prevention": r"预防措施|预警行动|监测预警",
    "response": r"应急响应|响应分级|响应程序",
    "disposal": r"处置方案|处置措施|现场处置|应急处置",
    "support": r"应急保障|保障措施",
    "end": r"应急结束|响应终止",
    "supplement": r"附则",
}


def select_segment_classification_text(record: dict) -> tuple[str, str, bool]:
    """Return local text, source field, and whether parent context was used."""
    for field in ("raw_text", "segment_text", "extracted_segment_text"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value, field, False
    # Compatibility-only fallback for legacy rows with no local field. This is
    # recorded explicitly so it cannot be mistaken for a local-text decision.
    value = record.get("normalized_text") or ""
    return value, "normalized_text_fallback", True


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _is_toc_line(line: str) -> bool:
    return bool(TOC_LINE_RE.match(line.strip()))


def _body_lines(text: str) -> list[str]:
    return [line for line in _nonempty_lines(text) if not _is_toc_line(line) and line not in {"目录", "目 录"}]


def has_continuous_body(text: str) -> bool:
    run = 0
    total = 0
    for line in _body_lines(text):
        chinese = len(re.findall(r"[\u4e00-\u9fff]", line))
        sentence_like = chinese >= 18 and (
            bool(re.search(r"[。；;]", line))
            or bool(re.search(r"应当|应立即|必须|不得|为了|发生|采取|负责|进行|包括", line))
        )
        total += int(sentence_like)
        run = run + 1 if sentence_like else 0
        if run >= 3:
            return True
    joined = "\n".join(_body_lines(text))
    return total >= 3 or (len(re.findall(r"[。；;]", joined)) >= 3 and len(joined) >= 160)


def numbered_action_count(text: str) -> int:
    count = 0
    for line in _body_lines(text):
        if re.match(r"^\s*(?:[（(]?\d+[）)、.．]|[一二三四五六七八九十]+[、.])", line) and ACTION_RE.search(line):
            count += 1
    return count


def structure_evidence(text: str) -> set[str]:
    return {name for name, pattern in STRUCTURE_PATTERNS.items() if re.search(pattern, text or "")}


def is_strict_toc_fragment(text: str) -> bool:
    lines = _nonempty_lines(text)
    if len(lines) < 3:
        return False
    toc_lines = [line for line in lines if _is_toc_line(line)]
    ratio = len(toc_lines) / len(lines)
    body = "\n".join(_body_lines(text))
    return bool(
        len(toc_lines) >= 3
        and ratio >= 0.55
        and not has_continuous_body(text)
        and not BODY_MARKER_RE.search(body)
        and len(re.findall(r"[。；;]", body)) < 3
        and numbered_action_count(text) < 2
        and len(ACTION_RE.findall(body)) < 3
    )


def has_full_document_structure(text: str) -> bool:
    evidence = structure_evidence(text)
    return len(evidence) >= 3 and has_continuous_body(text)


def has_onsite_disposal_structure(title: str, text: str) -> bool:
    if not ONSITE_TITLE_RE.search(title or ""):
        return False
    action_hits = len(ACTION_RE.findall(text or ""))
    inline_steps = len(re.findall(r"(?:^|[。；;\n])\s*\d+[、.．]", text or ""))
    continuous = has_continuous_body(text) or len(re.findall(r"[。；;]", text or "")) >= 3
    return len(text or "") >= 60 and continuous and (
        numbered_action_count(text) >= 2 or inline_steps >= 2 or action_hits >= 4
    )


def is_strict_list_fragment(text: str) -> bool:
    lines = _nonempty_lines(text)
    return bool(
        len(text or "") <= 700
        and len(lines) <= 4
        and len(structure_evidence(text)) < 2
        and not has_continuous_body(text)
        and len(FORMAL_HEADING_RE.findall(text or "")) < 2
    )


def classify_segment_v2421(record: dict, previous_stable_type: str = "") -> tuple[str, dict]:
    text, source, parent_used = select_segment_classification_text(record)
    title = record.get("original_title") or record.get("title") or ""
    base_type = record.get("segment_type") or "full_document"
    evidence = structure_evidence(text)
    full = has_full_document_structure(text)
    onsite = has_onsite_disposal_structure(title, text)
    toc = is_strict_toc_fragment(text)
    short_list = is_strict_list_fragment(text)

    # This hotfix is deliberately limited to the two regressed types. Existing
    # appendix/SDS/guidance/case/embedded classifications remain untouched.
    if base_type not in {"toc_fragment", "list_fragment"}:
        segment_type = base_type
    elif onsite:
        segment_type = "onsite_disposal_plan"
    elif full:
        segment_type = "full_document"
    elif toc:
        segment_type = "toc_fragment"
    elif base_type == "toc_fragment" and has_continuous_body(text):
        # A local正文 that failed the full-plan threshold still cannot remain a
        # directory merely because the parent context contains one.
        segment_type = previous_stable_type if previous_stable_type not in {"", "toc_fragment"} else "full_document"
    elif base_type == "list_fragment" and not short_list:
        segment_type = "full_document"
    else:
        segment_type = base_type

    audit = {
        "segment_classification_text_source": source,
        "segment_local_text_length": len(text),
        "parent_context_used": parent_used,
        "toc_line_count": sum(_is_toc_line(line) for line in _nonempty_lines(text)),
        "nonempty_line_count": len(_nonempty_lines(text)),
        "continuous_body": has_continuous_body(text),
        "numbered_action_count": numbered_action_count(text),
        "structure_evidence": sorted(evidence),
        "strict_toc_evidence": toc,
        "full_document_evidence": full,
        "onsite_disposal_evidence": onsite,
        "strict_list_evidence": short_list,
    }
    return segment_type, audit


def recover_hotfix_title(record: dict, segment_type: str, local_text: str) -> dict:
    original = (record.get("original_title") or "").strip()
    current = (record.get("title") or "").strip()
    lines = _nonempty_lines(local_text)[:50]

    if segment_type == "onsite_disposal_plan" and ONSITE_TITLE_RE.search(original):
        return {"title": original, "status": "valid", "source": "raw_text_heading", "evidence": ["raw_text以现场处置标题开始"]}

    if segment_type == "full_document":
        # Prefer a complete plan title printed as one line in the local text.
        for line in lines:
            candidate = line.strip("《》 ")
            if len(candidate) <= 80 and re.search(r"(?:专项应急预案|生产安全事故应急预案)$", candidate):
                if re.search(r"负责|组织|制定|实施|总指挥", candidate):
                    continue
                previous = lines[lines.index(line) - 1] if lines.index(line) > 0 else ""
                if previous.endswith(("街道办事处", "人民政府", "管理委员会")) and len(previous + candidate) <= 80:
                    short_issuer = re.search(r"([\u4e00-\u9fff]{2,16}(?:街道办事处|人民政府|管委会|管理委员会))$", previous)
                    if short_issuer:
                        candidate = short_issuer.group(1) + candidate
                return {"title": candidate, "status": "valid", "source": "raw_text_document_title", "evidence": ["raw_text前部正式预案标题"]}
        if current and len(current) <= 80 and re.search(r"应急预案$", current) and not re.search(r"总指挥|负责|组织|实施", current):
            return {"title": current, "status": "valid", "source": "existing_verified_title", "evidence": ["V2.4.2已验证标题"]}
        if re.search(r"有限空间", local_text[:1800]) and re.search(r"本应急预案|特制定本预案", local_text[:1800]):
            return {
                "title": "有限空间事故应急预案",
                "status": "uncertain",
                "source": "local_topic_fallback",
                "evidence": ["raw_text明确为有限空间预案，但缺少独立封面/H1标题"],
            }

    return {
        "title": current,
        "status": record.get("title_status", "uncertain"),
        "source": record.get("title_source", "source_record"),
        "evidence": record.get("title_recovery_evidence", []),
    }


__all__ = [
    "select_segment_classification_text", "is_strict_toc_fragment",
    "has_full_document_structure", "has_onsite_disposal_structure",
    "is_strict_list_fragment", "classify_segment_v2421", "recover_hotfix_title",
]
