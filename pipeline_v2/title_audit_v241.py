from __future__ import annotations

import re

from .classification_v23 import category_v23


SPECIAL_HEADING_RE = re.compile(
    r"(?m)^\s*(?:(?:第?[一二三四五六七八九十百]+|\d+)[、.．章节\s]*)?"
    r"[^\n。；;]{3,100}(?:专项应急预案|专项预案|现场处置方案)\s*$"
)


def _strip_repeated_title(title: str, text: str) -> str:
    body = (text or "").lstrip()
    if title and body.startswith(title):
        body = body[len(title):].lstrip()
    return body


def _topic_set(text: str) -> set[str]:
    patterns = {
        "chemical_leakage": r"危险化学品泄漏|危化品泄漏|燃气泄漏|泄漏事故",
        "confined_space": r"有限空间|受限空间",
        "poisoning_asphyxia": r"中毒|窒息|一氧化碳|硫化氢|缺氧",
        "fire_explosion": r"火灾|爆炸|燃烧",
        "natural_disaster": r"防汛|洪水|水灾|地震|台风|自然灾害",
        "major_hazard": r"重大危险源",
        "transport_accident": r"运输事故|交通事故|车辆事故",
    }
    return {name for name, pattern in patterns.items() if re.search(pattern, text or "")}


def audit_title_consistency_v241(
    *,
    document_id: str,
    title: str,
    text: str,
    segment_type: str,
    title_status: str,
    title_source: str,
    sections: list[dict],
) -> dict:
    body = _strip_repeated_title(title, text)
    body_probe = body[:500]
    first_heading = sections[0]["heading"] if sections else ""
    special_headings = [match.group(0).strip() for match in SPECIAL_HEADING_RE.finditer(body)]
    title_topics = _topic_set(title)
    body_topics = _topic_set(body_probe)
    first_heading_topics = _topic_set(first_heading)
    reasons: list[str] = []

    if segment_type in {"appendix_sds", "embedded_sds", "table", "toc_fragment"}:
        status = "not_applicable"
        reasons.append("segment_type_not_subject_to_accident_title_check")
    elif title_status != "valid":
        status = "uncertain"
        reasons.append("title_status_uncertain")
    elif title_source in {"source_record", "residual_fragment"}:
        status = "uncertain"
        reasons.append("title_from_residual_or_unverified_source")
    elif len(special_headings) >= 2:
        status = "uncertain"
        reasons.append("multiple_peer_special_headings")
    elif first_heading_topics and title_topics and not (first_heading_topics & title_topics):
        status = "inconsistent"
        reasons.append("first_real_heading_topic_conflicts_with_title")
    elif segment_type == "accident_case_section" and not re.search(
        r"事故经过|事故原因|直接原因|间接原因|事故教训|伤亡|死亡|受伤|中毒事故|事故调查",
        body_probe,
    ):
        status = "inconsistent"
        reasons.append("accident_case_title_but_body_is_not_case_narrative")
    elif title_topics and body_topics and not (title_topics & body_topics):
        # Confined-space cases commonly manifest as poisoning/asphyxia; this is
        # a compatible parent-risk relationship rather than a contradiction.
        compatible = title_topics == {"confined_space"} and bool(body_topics & {"poisoning_asphyxia"})
        if "chemical_leakage" in title_topics and re.search(r"危险化学品|危化品|化学品|CAS\s*(?:号|No)|燃气|天然气|储罐|管道", body_probe, re.I):
            compatible = True
        if compatible:
            status = "consistent"
            reasons.append("title_and_domain_risk_evidence_are_compatible")
        else:
            status = "inconsistent"
            reasons.append("body_first_500_topic_conflicts_with_title")
    elif segment_type in {"reference_sentence", "list_fragment"}:
        status = "uncertain"
        reasons.append("list_or_reference_sentence_requires_context_review")
    elif not title_topics and segment_type in {"full_document", "guidance_section"}:
        status = "not_applicable"
        reasons.append("non_accident_nominal_title")
    else:
        status = "consistent"
        reasons.append("no_topic_or_boundary_conflict_found")

    return {
        "document_id": document_id,
        "status": status,
        "title_category": category_v23(title, ""),
        "body_first_500_category": category_v23("", body_probe),
        "first_real_heading": first_heading,
        "peer_special_headings": special_headings,
        "title_source": title_source,
        "reasons": reasons,
        "review_status": "pending",
    }
