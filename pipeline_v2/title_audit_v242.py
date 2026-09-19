"""V2.4.2 title validation and title/body consistency audit."""
from __future__ import annotations

import re

from .classification_v23 import category_v23


def _topics(text: str) -> set[str]:
    patterns = {
        "chemical_leakage": r"危险化学品泄漏|危化品泄漏|泄漏事故|燃气泄漏|天然气|储罐|管道|化学品",
        "confined_space": r"有限空间|受限空间",
        "poisoning_asphyxia": r"中毒|窒息|一氧化碳|硫化氢|缺氧",
        "fire_explosion": r"火灾|爆炸|燃烧",
        "natural_disaster": r"防汛|洪水|水灾|地震|台风|自然灾害",
        "transport_accident": r"运输事故|交通事故|车辆事故",
    }
    return {key for key, pattern in patterns.items() if re.search(pattern, text or "")}


def _strip_title(title: str, text: str) -> str:
    body = (text or "").lstrip()
    if title and body.startswith(title):
        return body[len(title):].lstrip()
    return body


def title_checks(title: str, text: str, title_status: str, title_source: str, segment_type: str) -> dict:
    title = (title or "").strip()
    checks = {
        "over_80_chars": len(title) > 80,
        "contains_toc_dots": bool(re.search(r"(?:\.{5,}|…{3,}|-{4,})", title)),
        "multiple_toc_entries": len(re.findall(r"\d+(?:\.\d+){0,3}\s*[^\n]{2,40}(?:专项应急预案|现场处置方案)", title)) >= 2,
        "contains_body_sentence": bool(re.search(r"(?:一旦发生|作业现场负责人|组织相关人员|需要立即就医|按照本预案|负责组织|现印发给你们|请认真贯彻执行)", title)),
        "notification_fulltext": bool(re.search(r"(?:现印发|请认真贯彻执行|附件：|关于发布).{20,}", title)),
        "publication_note": bool(re.search(r"(?:已经.{0,30}(?:审议通过|同意|批准)|现予公布|自.{0,20}起施行)", title)),
        "first_aid_sentence": bool(re.search(r"(?:需要立即就医|向现场的医生|立即呼叫医生|出示此安全技术说明书)", title)),
        "residual_fragment": bool(re.match(r"^(?:速按照|例[》»]?等规定|发生因|需要立即|采取桌面|组织相关|负责制定)", title)) or bool(re.search(r"\d+小时(?:以上|以下)?$", title)),
        "title_source_unverified": title_source in {"source_record", "sds_section_fallback", "missing"},
        "status_invalid_or_missing": title_status in {"invalid", "missing"},
        "full_document_boundary_not_applicable": segment_type == "full_document",
    }
    return checks


def audit_title_consistency_v242(
    *,
    document_id: str,
    title: str,
    text: str,
    segment_type: str,
    title_status: str,
    title_source: str,
    sections: list[dict],
) -> dict:
    title = (title or "").strip()
    body = _strip_title(title, text)
    checks = title_checks(title, text, title_status, title_source, segment_type)
    title_topics = _topics(title)
    body_topics = _topics(body[:500])
    first_heading = sections[0].get("heading", "") if sections else ""
    peer_headings = re.findall(
        r"(?m)^\s*(?:第?[一二三四五六七八九十百]+[、.]|\d+[、.．])?\s*[^\n。；;]{3,100}(?:专项应急预案|专项预案|现场处置方案)\s*$",
        body,
    )
    reasons: list[str] = []
    if checks["contains_toc_dots"] or checks["multiple_toc_entries"]:
        status = "inconsistent"
        reasons.append("title_is_toc_or_toc_concatenation")
    elif checks["contains_body_sentence"] or checks["notification_fulltext"] or checks["publication_note"] or checks["first_aid_sentence"] or checks["residual_fragment"]:
        status = "inconsistent"
        reasons.append("title_contains_body_or_publication_sentence")
    elif title_status in {"invalid", "missing"}:
        status = "inconsistent"
        reasons.append("title_status_invalid_or_missing")
    elif segment_type == "toc_fragment" or title_status == "toc_only":
        status = "not_applicable"
        reasons.append("toc_only_not_subject_to_body_consistency")
    elif re.search(r"安全技术说明书|安全数据表|SDS|MSDS", title, re.I):
        status = "not_applicable"
        reasons.append("sds_title_not_subject_to_accident_topic_check")
    elif len(peer_headings) >= 2:
        status = "uncertain"
        reasons.append("multiple_peer_special_headings")
    elif segment_type == "accident_case_section" and not re.search(r"事故经过|事故原因|直接原因|间接原因|事故教训|伤亡|死亡|受伤|事故调查", body[:1200]):
        status = "inconsistent"
        reasons.append("accident_case_segment_lacks_case_narrative")
    elif title_topics and body_topics and not (title_topics & body_topics):
        compatible = title_topics == {"confined_space"} and bool(body_topics & {"poisoning_asphyxia"})
        if "chemical_leakage" in title_topics and re.search(r"危险化学品|危化品|化学品|CAS|燃气|天然气|储罐|管道", body[:500], re.I):
            compatible = True
        status = "consistent" if compatible else "inconsistent"
        reasons.append("compatible_domain_evidence" if compatible else "title_body_topic_conflict")
    elif segment_type in {"reference_sentence", "list_fragment"}:
        status = "uncertain"
        reasons.append("short_fragment_requires_context_review")
    elif not title_topics and segment_type in {"full_document", "guidance_section", "table"}:
        status = "not_applicable"
        reasons.append("non_accident_nominal_title")
    else:
        status = "consistent"
        reasons.append("no_title_body_conflict_found")
    if checks["full_document_boundary_not_applicable"]:
        reasons.append("full_document_boundary_not_applicable")
    return {
        "document_id": document_id,
        "status": status,
        "title_category": category_v23(title, ""),
        "body_first_500_category": category_v23("", body[:500]),
        "title": title,
        "title_source": title_source,
        "first_real_heading": first_heading,
        "peer_special_headings": peer_headings,
        "checks": checks,
        "reasons": reasons,
        "review_status": "pending",
    }


__all__ = ["title_checks", "audit_title_consistency_v242"]
