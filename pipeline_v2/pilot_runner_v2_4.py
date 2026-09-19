from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from .classification_v24 import (
    _find_sds_appendix_start,
    category_v24,
    is_sentence_title,
    lock_parent_type,
    recover_title_v24,
    segment_type_v24,
)
from .config import PILOT_SEED, PILOT_SIZE, SOURCE_RECORDS
from .knowledge_extractor import extract_knowledge
from .normalization import normalize_text
from .sds_matcher import sds_from_text
from .section_splitter_v24 import audit_heading, split_sections_v24


ROOT = SOURCE_RECORDS.parent.parent
CLEANED_V22 = ROOT / "cleaned_v2"
CLEANED_V23 = ROOT / "cleaned_v2_3"
CLEANED_V24 = ROOT / "cleaned_v2_4"
REPORTS_V23 = ROOT / "reports_v2_3"
REPORTS_V24 = ROOT / "reports_v2_4"
LOCKED = ROOT / "reports_v2" / "pilot_sample_manifest_locked_v2_1.json"
BEFORE = CLEANED_V23 / "pilot_100_results.json"


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def record_id(index: int, row: dict) -> str:
    seed = f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def parent_id(row: dict) -> str:
    identity = row.get("file") or row.get("url") or row.get("title") or "missing-parent"
    return "file-" + hashlib.sha256(str(identity).encode()).hexdigest()[:16]


def locked_rows(rows: list[dict]) -> list[tuple[int, dict, str]]:
    manifest = json.loads(LOCKED.read_text(encoding="utf-8"))
    by_id = {record_id(index, row): (index, row) for index, row in enumerate(rows)}
    selected = [(by_id[item["document_id"]][0], by_id[item["document_id"]][1], item.get("sampling_bucket", "locked")) for item in manifest["records"]]
    if len(selected) != PILOT_SIZE:
        raise RuntimeError(f"locked sample changed: {len(selected)}")
    return selected


def parent_context(items: list[tuple[int, dict, str]], parent: str) -> tuple[str, str, str]:
    parsed = CLEANED_V22 / "parsed_documents" / f"{parent}.json"
    parsed_text = json.loads(parsed.read_text(encoding="utf-8")).get("raw_text", "") if parsed.exists() else ""
    titles, contents, types = [], [], []
    for _, row, _ in items:
        title = (row.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
        content = normalize_text(row.get("content") or "")
        if content and content not in contents:
            contents.append(content)
        if row.get("type"):
            types.append(row["type"])
    joined = "\n\n".join(contents)
    text = parsed_text if len(parsed_text) >= max((len(x) for x in contents), default=0) else joined
    return "｜".join(titles)[:6000], text[:180000], Counter(types).most_common(1)[0][0] if types else ""


def _find_title_start(text: str, title: str) -> int:
    if not title:
        return -1
    # A title usually appears once in the contents and again at the actual
    # section start; use the last occurrence to avoid cutting from the TOC.
    direct = text.rfind(title)
    if direct >= 0:
        return direct
    compact_text = re.sub(r"[\s\u3000]", "", text)
    compact_title = re.sub(r"[\s\u3000]", "", title)
    index = compact_text.rfind(compact_title)
    if index < 0:
        return -1
    # Map compact offset back approximately; this is only used as a fallback
    # when a parser inserted spaces into a heading.
    seen = 0
    for position, char in enumerate(text):
        if not char.isspace() and char not in "\u3000":
            if seen == index:
                return position
            seen += 1
    return -1


def extract_parent_segment(parent_text: str, title: str) -> str:
    start = _find_title_start(parent_text, title)
    if start < 0:
        return ""
    tail = parent_text[start:]
    next_heading = re.search(r"(?m)^\s*(?:(?:[一二三四五六七八九十百]+[、.]|\d+[、.．])\s*)?[^\n。；;]{4,110}(?:专项应急预案|专项预案|现场处置方案|综合应急预案)\s*$", tail[len(title):])
    if next_heading:
        return tail[: len(title) + next_heading.start()].strip()
    return tail.strip()


def has_cross_special(text: str) -> bool:
    headings = re.findall(r"(?m)^\s*(?:[一二三四五六七八九十百]+[、.]|\d+[、.．])\s*[^\n]{4,110}(?:专项应急预案|专项预案|现场处置方案)", text)
    return len(headings) >= 2


def title_body_warning(title: str, text: str) -> bool:
    body = text.lstrip()
    if title and body.startswith(title):
        body = body[len(title):].lstrip()
    probe = body[:300]
    topic_patterns = {
        "confined_space": r"有限空间|受限空间|密闭空间|缺氧",
        "chemical_leakage": r"泄漏|危险化学品|危化品|化学品|燃气|天然气|储罐|CAS\s*(?:号|No)",
        "fire_explosion": r"火灾|爆炸|易燃|可燃|燃烧",
        "poisoning_asphyxia": r"中毒|窒息|有毒有害|硫化氢|缺氧",
        "natural_disaster": r"防汛|水灾|洪水|自然灾害|地震|台风",
        "major_hazard": r"重大危险源|危险源辨识",
    }
    title_topics = {topic for topic, pattern in topic_patterns.items() if re.search(pattern, title, re.I)}
    body_topics = {topic for topic, pattern in topic_patterns.items() if re.search(pattern, probe, re.I)}
    return bool(title_topics and body_topics and not (title_topics & body_topics))


def review_markdown(path: Path, heading: str, records: list[dict], parent: bool = False) -> None:
    lines = [f"# {heading}", "", "本审核包仅供人工复核；自动结果和审核状态均为 `pending`。", ""]
    for index, item in enumerate(records, 1):
        lines += [f"## {index}. {item.get('parent_document_id', item.get('document_id'))}", "", f"- 文件：{item.get('file_name', '')}", f"- 来源：{item.get('source_url', '') or '缺失'}"]
        if parent:
            lines += [f"- 父文档类型：{item.get('parent_document_type')}", f"- 类型已锁定：{item.get('parent_type_locked')}", f"- 证据范围：{item.get('parent_type_evidence_scope')}", f"- 嵌入内容：{item.get('embedded_content_types')}", f"- 证据：{json.dumps(item.get('classification_evidence', []), ensure_ascii=False)}", "- 人工类型：________", "- 人工备注：________"]
            if item.get("previous_parent_document_type"):
                lines.insert(-2, f"- V2.3父文档类型：{item.get('previous_parent_document_type')}")
        else:
            lines += [f"- 父文档类型：{item.get('parent_document_type')}", f"- 子章节类型：{item.get('segment_type')}", f"- 事故类别：{item.get('accident_category')}", f"- 标题：{item.get('title')}", f"- 警告：{item.get('warnings')}", "- 人工类型：________", "- 边界是否正确：________", "- 人工备注：________"]
            if item.get("high_risk_reasons"):
                lines.insert(-3, f"- 高风险原因：{item.get('high_risk_reasons')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parent_reviews(output: list[dict], locks: dict[str, object]) -> tuple[list[dict], list[dict], list[dict]]:
    grouped = defaultdict(list)
    for item in output:
        grouped[item["parent_document_id"]].append(item)
    all_records, enterprise_records, sds_records = [], [], []
    for parent, values in grouped.items():
        representative = max(values, key=lambda item: len(item.get("normalized_text", "")))
        lock = locks[parent]
        item = {
            "parent_document_id": parent,
            "representative_document_id": representative["document_id"],
            "file_name": Path(representative["source_file"]).name,
            "source_url": representative["source_url"],
            "parent_document_type": lock.result.parent_document_type,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "content_role": lock.result.content_role,
            "classification_confidence": lock.result.classification_confidence,
            "classification_evidence": lock.result.classification_evidence,
            "human_parent_type": "",
            "human_notes": "",
            "review_status": "pending",
        }
        all_records.append(item)
        if item["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"}:
            enterprise_records.append(item)
        if item["parent_document_type"] == "sds":
            sds_records.append(item)
    return all_records, enterprise_records, sds_records


def main() -> None:
    CLEANED_V24.mkdir(exist_ok=True)
    REPORTS_V24.mkdir(exist_ok=True)
    rows = json.loads(SOURCE_RECORDS.read_text(encoding="utf-8"))
    selected = locked_rows(rows)
    groups = defaultdict(list)
    for item in selected:
        groups[parent_id(item[1])].append(item)

    locks = {}
    contexts = {}
    for parent, items in groups.items():
        title, text, original_type = parent_context(items, parent)
        contexts[parent] = (title, text, original_type)
        locks[parent] = lock_parent_type(parent, title, text, original_type)

    output: list[dict] = []
    review_queue: list[dict] = []
    attachment_events: list[dict] = []
    for index, row, bucket in selected:
        ident, parent = record_id(index, row), parent_id(row)
        parent_title, parent_text, _ = contexts[parent]
        lock = locks[parent]
        original_title = (row.get("title") or "").strip()
        raw_text = row.get("content") or ""
        normalized = normalize_text(raw_text)
        title, recovered = recover_title_v24(original_title, normalized, parent_text)
        effective_text = extract_parent_segment(parent_text, title) if title else ""
        if len(effective_text) < max(200, int(len(normalized) * 0.25)):
            effective_text = normalized

        sds_start = _find_sds_appendix_start(effective_text) if lock.result.parent_document_type != "sds" else None
        appendix_text = ""
        if sds_start is not None and sds_start > 500 and len(effective_text) - sds_start > 700:
            appendix_text = effective_text[sds_start:].strip()
            effective_text = effective_text[:sds_start].rstrip()
            attachment_events.append({"source_document_id": ident, "parent_document_id": parent, "start_position": sds_start, "segment_type": "appendix_sds", "review_status": "pending"})

        segment_type = segment_type_v24(title, effective_text, lock.result.parent_document_type, len(groups[parent]))
        # Parent identity is already locked.  Do not infer an accident class
        # from SDS/regulation body words such as “fire” or “leakage”.
        category = (
            "other"
            if lock.result.parent_document_type in {"sds", "chemical_catalog", "regulation"}
            else category_v24(title, effective_text)
        )
        warnings: list[str] = []
        if recovered:
            warnings.append("sentence_used_as_title" if is_sentence_title(original_title) else "title_recovered_from_parent_parse")
        if appendix_text:
            warnings.append("attachment_overrode_parent_type")
        if title_body_warning(title, effective_text):
            warnings.append("title_body_mismatch")
        if has_cross_special(effective_text):
            warnings.append("cross_special_plan_contamination")
        sections = split_sections_v24(effective_text, category) if segment_type not in {"list_fragment", "reference_sentence", "toc_fragment"} else []
        for section in sections:
            audit = audit_heading(section.heading, section.content, category)
            if audit:
                section.warnings.extend(audit)
                warnings.extend(audit)
        if warnings and any(item in warnings for item in ("title_body_mismatch", "cross_special_plan_contamination", "sentence_used_as_title")):
            warnings.append("boundary_error")
        role = lock.result.content_role
        if segment_type == "onsite_disposal_plan":
            role = "disposal_knowledge"
        elif segment_type in {"appendix_sds", "embedded_sds"}:
            role = "safety_data"
        elif segment_type in {"list_fragment", "reference_sentence"}:
            role = "reference_sentence"
        knowledge = [item.model_dump() for item in extract_knowledge(ident, row.get("url") or "", sections)]
        sds = sds_from_text(ident, effective_text, row.get("url") or "") if lock.result.parent_document_type == "sds" else None
        requires_review = bool(lock.result.requires_manual_review or warnings)
        quality = "low_quality" if lock.result.parent_document_type == "sds" and lock.result.requires_manual_review else "manual_review" if requires_review else "silver_candidate"
        record = {
            "document_id": ident,
            "parent_document_id": parent,
            "source_file": row.get("file") or "[missing]",
            "source_url": row.get("url") or "",
            "title": title,
            "original_title": original_title,
            "raw_text": raw_text,
            "normalized_text": effective_text,
            "parent_document_type": lock.result.parent_document_type,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "segment_type": segment_type,
            "accident_category": category,
            "content_role": role,
            "classification_reasons": lock.result.classification_reasons,
            "classification_evidence": lock.result.classification_evidence,
            "classification_confidence": lock.result.classification_confidence,
            "requires_manual_review": requires_review,
            "sections": [section.model_dump() for section in sections],
            "knowledge_items": knowledge,
            "sds": sds.model_dump() if sds else None,
            "warnings": list(dict.fromkeys(warnings)),
            "sampling_bucket": bucket,
            "quality_grade": quality,
            "review_status": "pending",
        }
        output.append(record)
        if requires_review:
            review_queue.append({"document_id": ident, "parent_document_id": parent, "stage": "v24_parent_or_boundary", "reason": list(dict.fromkeys(warnings or lock.result.classification_reasons)), "review_status": "pending"})

        if appendix_text:
            appendix_id = ident + "-appendix-sds"
            appendix_sds = sds_from_text(appendix_id, appendix_text, row.get("url") or "")
            appendix_sections = split_sections_v24(appendix_text, "other")
            output.append({
                "document_id": appendix_id,
                "parent_document_id": parent,
                "source_file": row.get("file") or "[missing]",
                "source_url": row.get("url") or "",
                "title": "SDS附件",
                "original_title": original_title,
                "raw_text": appendix_text,
                "normalized_text": appendix_text,
                "parent_document_type": lock.result.parent_document_type,
                "parent_type_locked": True,
                "parent_type_evidence_scope": lock.evidence_scope,
                "embedded_content_types": lock.embedded_content_types,
                "segment_type": "appendix_sds",
                "accident_category": "other",
                "content_role": "safety_data",
                "classification_reasons": ["SDS位于企业预案正文之后，单独拆分"],
                "classification_evidence": [],
                "classification_confidence": 0.84,
                "requires_manual_review": True,
                "sections": [section.model_dump() for section in appendix_sections],
                "knowledge_items": [],
                "sds": appendix_sds.model_dump(),
                "warnings": ["attachment_overrode_parent_type", "review_status_pending"],
                "sampling_bucket": bucket,
                "quality_grade": "manual_review",
                "review_status": "pending",
            })

    dump(CLEANED_V24 / "pilot_100_results.json", output)
    dump(CLEANED_V24 / "pilot_100_manual_review_queue.json", review_queue)

    before_rows = json.loads(BEFORE.read_text(encoding="utf-8"))
    before = {item["document_id"]: item for item in before_rows}
    before_parent_counts: dict[str, Counter] = defaultdict(Counter)
    for item in before_rows:
        before_parent_counts[item.get("parent_document_id", "")][item.get("parent_document_type", "")] += 1
    before_parent_types = {
        parent: counts.most_common(1)[0][0]
        for parent, counts in before_parent_counts.items()
        if parent and counts
    }

    all_parents, enterprise_parents, sds_parents = build_parent_reviews(output, locks)
    dump(REPORTS_V24 / "manual_review_all_parents_49.json", all_parents)
    dump(REPORTS_V24 / "manual_review_enterprise_parents.json", enterprise_parents)
    dump(REPORTS_V24 / "manual_review_sds_parents.json", sds_parents)
    review_markdown(REPORTS_V24 / "manual_review_all_parents_49.md", "V2.4全部49个父文档审核包", all_parents, True)
    review_markdown(REPORTS_V24 / "manual_review_enterprise_parents.md", "V2.4企业预案父文档审核包", enterprise_parents, True)
    review_markdown(REPORTS_V24 / "manual_review_sds_parents.md", "V2.4独立SDS父文档审核包", sds_parents, True)

    # The V2.4 acceptance package also requires 30 distinct parent documents,
    # rather than 30 child rows that may repeat the same parent.  Prefer a
    # type-stratified deterministic order, then fill from the remaining locks.
    parent_priority = {
        "enterprise_plan": 0,
        "enterprise_group_plan": 1,
        "sds": 2,
        "government_or_regional_plan": 3,
        "organization_plan": 4,
        "guidance_reference": 5,
        "accident_case": 6,
        "regulation": 7,
        "chemical_catalog": 8,
        "manual_review": 9,
    }
    parent_30 = sorted(
        all_parents,
        key=lambda item: (parent_priority.get(item["parent_document_type"], 99), item["parent_document_id"]),
    )[:30]
    dump(REPORTS_V24 / "manual_review_parent_30.json", parent_30)
    review_markdown(REPORTS_V24 / "manual_review_parent_30.md", "V2.4不同父文档审核包（30个）", parent_30, True)

    changed_parent_reviews = []
    for item in all_parents:
        old_type = before_parent_types.get(item["parent_document_id"], "")
        if old_type and old_type != item["parent_document_type"]:
            changed = dict(item)
            changed["previous_parent_document_type"] = old_type
            changed_parent_reviews.append(changed)
    dump(REPORTS_V24 / "manual_review_changed_parents.json", changed_parent_reviews)
    review_markdown(REPORTS_V24 / "manual_review_changed_parents.md", "V2.4父文档类型变化审核包", changed_parent_reviews, True)

    enterprise_government_conflict_parents = set()
    for parent, (_, parent_text, _) in contexts.items():
        probe = parent_text[: max(9000, min(len(parent_text), int(len(parent_text) * 0.2) or len(parent_text)))]
        enterprise_evidence = bool(re.search(r"有限公司|股份有限公司|我公司|本公司|本单位|公司厂内|厂区|车间|储罐区|公司应急救援指挥部|公司总经理|适用于本公司", probe))
        government_evidence = bool(re.search(r"人民政府|管委会|应急管理局|安委会|上级应急管理部门|属地政府|请求政府支援|政府调查", probe))
        if enterprise_evidence and government_evidence:
            enterprise_government_conflict_parents.add(parent)

    high_risk = []
    for item in output:
        reasons: list[str] = []
        warnings = item.get("warnings", [])
        old_item = before.get(item["document_id"], {})
        if "title_body_mismatch" in warnings:
            reasons.append("title_body_mismatch")
        elif (
            old_item
            and old_item.get("title") != item.get("title")
            and old_item.get("accident_category") != item.get("accident_category")
        ):
            reasons.append("title_body_mismatch_corrected_candidate")
        if "sentence_used_as_title" in warnings:
            reasons.append("sentence_used_as_title")
        if "cross_special_plan_contamination" in warnings:
            reasons.append("cross_special_plan_contamination")
        if item.get("segment_type") in {"appendix_sds", "embedded_sds"} or "appendix_sds" in item.get("embedded_content_types", []):
            reasons.append("parent_contains_sds_attachment")
        if item.get("parent_document_id") in enterprise_government_conflict_parents:
            reasons.append("enterprise_and_government_evidence_coexist")
        if not reasons:
            continue
        risk_item = dict(item)
        risk_item["high_risk_reasons"] = list(dict.fromkeys(reasons))
        high_risk.append(risk_item)
    dump(REPORTS_V24 / "manual_review_high_risk.json", high_risk)
    review_markdown(REPORTS_V24 / "manual_review_high_risk.md", "V2.4高风险错误审核包", high_risk, False)

    rng = random.Random(PILOT_SEED + 2401)
    segment_candidates = sorted(output, key=lambda item: (not bool(item.get("warnings")), item.get("segment_type") == "full_document"))
    rng.shuffle(segment_candidates)
    segment_candidates.sort(key=lambda item: (not bool(item.get("warnings")), item.get("segment_type") == "full_document"))
    segment_review, seen = [], Counter()
    for item in segment_candidates:
        if seen[item["parent_document_id"]] >= 2:
            continue
        segment_review.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "file_name": Path(item["source_file"]).name, "source_url": item["source_url"], "parent_document_type": item["parent_document_type"], "segment_type": item["segment_type"], "accident_category": item["accident_category"], "title": item["title"], "warnings": item["warnings"], "review_status": "pending", "human_segment_type": "", "human_boundary_correct": None, "human_notes": ""})
        seen[item["parent_document_id"]] += 1
        if len(segment_review) == 30:
            break
    dump(REPORTS_V24 / "manual_review_segment_30.json", segment_review)
    review_markdown(REPORTS_V24 / "manual_review_segment_30.md", "V2.4子章节边界审核包（30条）", segment_review, False)

    parent_changes, segment_changes = [], []
    for item in output:
        old = before.get(item["document_id"], {})
        old_parent = old.get("parent_document_type", "")
        if old_parent and old_parent != item["parent_document_type"]:
            parent_changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before": {"parent_document_type": old_parent, "title": old.get("title", "")}, "after": {"parent_document_type": item["parent_document_type"], "title": item["title"], "parent_type_locked": True}, "review_status": "pending"})
        old_view = {key: old.get(key, "") for key in ("segment_type", "accident_category", "title")}
        new_view = {key: item.get(key, "") for key in ("segment_type", "accident_category", "title")}
        if old_view != new_view or item.get("warnings"):
            segment_changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before": old_view, "after": new_view, "warnings": item.get("warnings", []), "review_status": "pending"})
    dump(REPORTS_V24 / "parent_type_changes_v2_4.json", parent_changes)
    dump(REPORTS_V24 / "segment_changes_v2_4.json", segment_changes)

    lock_audit = [{"parent_document_id": parent, "parent_document_type": lock.result.parent_document_type, "parent_type_locked": True, "parent_type_evidence_scope": lock.evidence_scope, "cover_title": lock.cover_title, "classification_evidence": lock.result.classification_evidence, "embedded_content_types": lock.embedded_content_types, "review_status": "pending"} for parent, lock in locks.items()]
    dump(REPORTS_V24 / "parent_lock_audit_v2_4.json", lock_audit)
    dump(REPORTS_V24 / "sds_attachment_split_v2_4.json", attachment_events)
    (REPORTS_V24 / "sds_attachment_split_v2_4.md").write_text("# V2.4 SDS附件拆分报告\n\n" + f"- 发现并拆分附件：{len(attachment_events)}条\n- SDS附件不覆盖父文档类型，所有拆分记录保持 `pending`。\n", encoding="utf-8")

    audit_rows = []
    for item in output:
        for section in item.get("sections", []):
            reasons = audit_heading(section["heading"], section.get("content", ""), item.get("accident_category", "other"))
            if reasons:
                audit_rows.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "heading": section["heading"], "reasons": reasons, "review_status": "pending"})
    dump(REPORTS_V24 / "fake_heading_audit_v2_4.json", audit_rows)
    (REPORTS_V24 / "fake_heading_audit_v2_4.md").write_text("# V2.4假标题独立审计报告\n\n" + f"- 保留标题的独立审计命中：{len(audit_rows)}条；命中即进入人工审核。\n- 审计规则：超过30个汉字、完整谓语宾语、规定的句首动词、时间金额数量、标题/正文前300字主题、同级专项标题。\n- 本数值仅表示独立审计命中量，不代表人工确认错误数或总体错误率。\n", encoding="utf-8")

    mismatch = [{"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "title": item["title"], "warnings": item["warnings"], "review_status": "pending"} for item in output if "title_body_mismatch" in item.get("warnings", [])]
    dump(REPORTS_V24 / "title_body_consistency_v2_4.json", mismatch)
    (REPORTS_V24 / "title_body_consistency_v2_4.md").write_text("# V2.4标题正文一致性报告\n\n" + f"- 标题/正文不一致：{len(mismatch)}条。\n- 结果需人工确认，未自动批准或删除。\n", encoding="utf-8")

    # Explicit quality and accident-category audits keep these dimensions
    # reviewable without pretending that automatic labels are approved.
    sds_audit = [
        {
            "document_id": item["document_id"],
            "parent_document_id": item["parent_document_id"],
            "parent_document_type": item["parent_document_type"],
            "segment_type": item["segment_type"],
            "title": item["title"],
            "quality_grade": item["quality_grade"],
            "requires_manual_review": item["requires_manual_review"],
            "warnings": item["warnings"],
            "source_url": item["source_url"],
            "review_status": "pending",
        }
        for item in output
        if item["parent_document_type"] == "sds" or item["segment_type"] in {"appendix_sds", "embedded_sds"}
    ]
    dump(REPORTS_V24 / "sds_quality_audit_v2_4.json", sds_audit)
    sds_quality_counts = Counter(item["quality_grade"] for item in sds_audit)
    sds_lines = [
        "# V2.4 SDS质量分级报告",
        "",
        f"- SDS父文档及SDS附件记录：{len(sds_audit)}条。",
        f"- 质量分级计数：{dict(sds_quality_counts)}。",
        "- 自动分级仅为候选标签；所有记录保持 `pending`，不得视为人工批准。",
        "",
        "## 明细",
        "",
    ]
    for item in sds_audit:
        sds_lines.append(
            f"- `{item['document_id']}`｜父类型 `{item['parent_document_type']}`｜子类型 `{item['segment_type']}`｜"
            f"质量 `{item['quality_grade']}`｜需复核 `{item['requires_manual_review']}`｜来源 `{item['source_url'] or '缺失'}`"
        )
    (REPORTS_V24 / "sds_quality_report_v2_4.md").write_text("\n".join(sds_lines) + "\n", encoding="utf-8")

    category_audit = [
        {
            "document_id": item["document_id"],
            "parent_document_id": item["parent_document_id"],
            "parent_document_type": item["parent_document_type"],
            "segment_type": item["segment_type"],
            "title": item["title"],
            "accident_category": item["accident_category"],
            "classification_evidence": item["classification_evidence"],
            "warnings": item["warnings"],
            "review_status": "pending",
        }
        for item in output
    ]
    dump(REPORTS_V24 / "accident_category_audit_v2_4.json", category_audit)
    category_counts = Counter(item["accident_category"] for item in category_audit)
    category_lines = [
        "# V2.4事故类别审计报告",
        "",
        f"- 结构化记录：{len(category_audit)}条（含1条拆分SDS附件）。",
        f"- 事故类别计数：{dict(category_counts)}。",
        "- 类别按标题、适用范围、风险分析和处置对象优先判定；正文关键词仅作辅助。",
        "- 全部自动结果保持 `pending`，类别准确率需人工审核包确认。",
        "",
        "## 需要重点复核的记录",
        "",
    ]
    for item in category_audit:
        if item["warnings"] or item["accident_category"] in {"multi_hazard", "major_hazard", "other"}:
            category_lines.append(
                f"- `{item['document_id']}`｜`{item['parent_document_type']}`/`{item['segment_type']}`｜"
                f"`{item['accident_category']}`｜{item['title']}｜警告：{item['warnings'] or '无'}"
            )
    (REPORTS_V24 / "accident_category_audit_v2_4.md").write_text("\n".join(category_lines) + "\n", encoding="utf-8")

    sds_attachment_repaired_parents = {
        parent
        for parent, lock in locks.items()
        if before_parent_types.get(parent) == "sds"
        and lock.result.parent_document_type != "sds"
        and "appendix_sds" in lock.embedded_content_types
    }
    enterprise_government_repaired_parents = {
        parent
        for parent, lock in locks.items()
        if before_parent_types.get(parent) == "government_or_regional_plan"
        and lock.result.parent_document_type in {"enterprise_plan", "enterprise_group_plan"}
    }
    sentence_false_title_ids = {
        item["document_id"]
        for item in output
        if "sentence_used_as_title" in item.get("warnings", [])
    }
    sentence_false_title_ids.update(
        item["document_id"]
        for item in audit_rows
        if "sentence_like_heading" in item.get("reasons", [])
    )
    repair_metrics = {
        "enterprise_plan_count": sum(1 for lock in locks.values() if lock.result.parent_document_type == "enterprise_plan"),
        "enterprise_group_plan_count": sum(1 for lock in locks.values() if lock.result.parent_document_type == "enterprise_group_plan"),
        "standalone_sds_parent_count": sum(1 for lock in locks.values() if lock.result.parent_document_type == "sds"),
        "appendix_sds_count": sum(1 for item in output if item["segment_type"] == "appendix_sds"),
        "sds_attachment_overwrite_repaired_parent_count": len(sds_attachment_repaired_parents),
        "enterprise_misclassified_as_government_repaired_parent_count": len(enterprise_government_repaired_parents),
        "title_body_mismatch_count": len(mismatch),
        "sentence_false_title_count": len(sentence_false_title_ids),
        "changed_parent_count": len(changed_parent_reviews),
        "high_risk_review_record_count": len(high_risk),
        "review_status": "pending",
    }
    dump(REPORTS_V24 / "repair_metrics_v2_4.json", repair_metrics)

    pytest_text = (REPORTS_V24 / "pytest_v2_4.txt").read_text(encoding="utf-8") if (REPORTS_V24 / "pytest_v2_4.txt").exists() else "测试输出待刷新"
    (REPORTS_V24 / "regression_test_report_v2_4.md").write_text("# V2.4回归测试报告\n\n```text\n" + pytest_text.strip() + "\n```\n", encoding="utf-8")
    ptypes = Counter(lock.result.parent_document_type for lock in locks.values())
    stypes = Counter(item["segment_type"] for item in output)
    report = ["# V2.4固定100条试运行报告", "", f"固定种子：`{PILOT_SEED}`；沿用V2.1锁定清单，输入100条，未重新抽样。", "", "## 统计", "", f"- 输入源记录：{len(selected)}", f"- 结构化记录（含拆分附件）：{len(output)}", f"- 独立父文档：{len(locks)}", f"- 父文档类型：{dict(ptypes)}", f"- 子章节类型：{dict(stypes)}", f"- enterprise_plan数量：{ptypes.get('enterprise_plan', 0)}", f"- enterprise_group_plan数量：{ptypes.get('enterprise_group_plan', 0)}", f"- 独立SDS父文档数量：{ptypes.get('sds', 0)}", f"- appendix_sds数量：{stypes.get('appendix_sds', 0)}", f"- 被SDS附件错误覆盖后修复的父文档数量：{len(sds_attachment_repaired_parents)}", f"- 企业误判为政府预案的修复数量：{len(enterprise_government_repaired_parents)}", f"- 父文档类型发生变化的父文档数量：{len(changed_parent_reviews)}", f"- 父文档类型变化记录：{len(parent_changes)}", f"- 子章节变化记录：{len(segment_changes)}", f"- 标题正文不一致数量：{len(mismatch)}", f"- 句子型假标题数量：{len(sentence_false_title_ids)}", f"- 保留标题的独立假标题审计命中：{len(audit_rows)}", f"- 高风险错误审核记录：{len(high_risk)}", "- 父文档人工审核包：全部49个父文档；另生成父类型变化、企业预案和SDS专项审核包。", "- 子章节人工审核包：30条（每条保持pending；允许同一父文档出现多个子章节）。", "", "## 验收状态", "", "- 父文档准确率：待全部49个父文档人工审核。", "- 子章节类型准确率：待人工审核。", "- 子章节边界准确率：待人工审核。", "- 标题正文一致性：待人工审核。", "- SDS父文档/附件拆分准确率：待人工审核。", "", "本轮仅完成V2.4定点修复和固定100条复验；所有结果保持 `pending`，不具备全量运行条件。"]
    (REPORTS_V24 / "pilot_100_report_v2_4.md").write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
