"""Final title and false-exclusion repair before database ingestion.

This runner reuses the semantic-fixed parse checkpoints and canonical segment
relations.  It never parses the full raw corpus, writes under ``raw``, creates
embeddings, imports a database, or changes ``pending`` review status.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .full_runner_v1 import split_for_chunks, stable_id
from .models_title_fixed_v1 import TitleFixedChunk, TitleFixedParent, TitleFixedSegment
from .repair_full_v1 import (
    clean_preview,
    dump_json,
    dump_jsonl,
    load_jsonl,
    now,
    review_package,
    run_pytest,
    stratified_sample,
    write_md,
)
from .repair_rules_v1 import text_hash
from .semantic_fix_v1 import parsed_checkpoint, raw_content_inventory, raw_integrity_by_content
from .semantic_rules_v1 import HTML_TAG_RE, SCRIPT_RE, has_navigation_or_script_pollution, recover_parent_title_semantic, strong_sds_evidence
from .title_fix_rules_v1 import (
    FIELD_STICK_RE,
    SECTION_DATE_ONLY_RE,
    SECTION_DATE_SENTENCE_RE,
    PERSON_ROLE_RE,
    audit_section_title,
    parent_title_issues,
    reclassify_excluded,
    recover_enterprise_title,
    recover_sds_title,
)


ROOT = Path(__file__).resolve().parents[1]
SEED = 20260824
CONTEXT_SEGMENTS = {
    "front_matter", "metadata_section", "personnel_table", "contact_list",
    "toc_fragment", "reference_sentence", "list_fragment",
}
NO_RAG_PARENT_TYPES = {"excluded_irrelevant", "manual_review", "chemical_reference"}


def title_checkpoint(root: Path, document_id: str) -> tuple[dict[str, Any], str]:
    semantic = root / "checkpoints_full_v1_semantic_fixed" / "parsed_documents" / f"{document_id}.json"
    if semantic.exists():
        return json.loads(semantic.read_text(encoding="utf-8")), str(semantic)
    return parsed_checkpoint(root, document_id)


def parsed_text(checkpoint: dict[str, Any]) -> str:
    return str(checkpoint.get("normalized_text") or checkpoint.get("raw_text") or "")


def role_for(parent_type: str, segment_type: str | None = None, preferred: str = "") -> str:
    if preferred and parent_type not in {"sds", "accident_case", "regulation", "guidance_reference", "excluded_irrelevant", "manual_review"}:
        return preferred
    if parent_type == "sds": return "safety_data_section" if segment_type and segment_type != "full_document" else "safety_data"
    if parent_type == "accident_case": return "accident_case_reference"
    if parent_type == "regulation": return "regulatory_article" if segment_type == "regulation_article" else "regulatory_reference"
    if parent_type == "guidance_reference": return preferred or "guidance_reference"
    if parent_type == "excluded_irrelevant": return "excluded_irrelevant"
    if parent_type == "manual_review": return "manual_review"
    if parent_type == "chemical_catalog": return "chemical_catalog"
    return "emergency_plan"


def general_title(parent: dict[str, Any], text: str, html_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    current = str(parent.get("parent_title") or "")
    ptype = str(parent.get("parent_document_type") or "")
    if not parent_title_issues(current, ptype):
        return {"title": current, "status": "valid", "source": str(parent.get("parent_title_source") or "existing_valid_title"), "issues": []}
    recovered = recover_parent_title_semantic(parent, text, html_meta)
    candidate = str(recovered.get("title") or "")
    issues = parent_title_issues(candidate, ptype)
    if candidate and not issues:
        return {"title": candidate, "status": "valid", "source": recovered.get("source") or "parsed_document_title", "issues": []}
    return {"title": "", "status": "missing", "source": "unresolved_after_quality_validation", "issues": issues or ["no_reliable_document_title"]}


def transform_parent(old: dict[str, Any], text: str, checkpoint: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    row = dict(old)
    old_type = str(old.get("parent_document_type") or "")
    decision: dict[str, Any] | None = None
    if old_type == "excluded_irrelevant":
        decision = reclassify_excluded(row, text)
        row.update({
            "parent_document_type": decision["parent_document_type"],
            "classification_confidence": decision["confidence"],
            "classification_evidence": decision["evidence"],
            "classification_reasons": [f"excluded_recheck_from_{old_type}_to_{decision['parent_document_type']}"],
            "requires_manual_review": decision["requires_manual_review"],
            "rag_enabled": decision["rag_enabled"],
            "content_role": decision["content_role"],
        })

    new_type = str(row.get("parent_document_type") or "")
    html_meta = {"title": checkpoint.get("title", ""), "h1": checkpoint.get("h1", "")}
    if new_type == "sds":
        recovered = recover_sds_title(text, row["source_file"])
        row["chemical_name"] = recovered["chemical_name"]
    elif new_type in {"enterprise_plan", "enterprise_group_plan"}:
        recovered = recover_enterprise_title(row, text)
    else:
        recovered = general_title(row, text, html_meta)

    title = recovered["title"]
    status = recovered["status"]
    title_issues = parent_title_issues(title, new_type)
    if title_issues:
        # An unresolved title is safer than preserving a body sentence or a
        # field-stuck value.  SDS and enterprise records have explicit generic
        # pending-review fallbacks authorized by the task specification.
        if new_type == "sds":
            title = "化学品安全技术说明书"
            status = "uncertain"
            recovered["source"] = "generic_sds_title_after_quality_rejection"
        elif new_type in {"enterprise_plan", "enterprise_group_plan"}:
            title = "企业生产安全事故应急预案"
            status = "uncertain"
            recovered["source"] = "generic_enterprise_title_after_quality_rejection"
        else:
            title = ""
            status = "missing"
            recovered["source"] = "unresolved_after_quality_validation"
        title_issues = parent_title_issues(title, new_type)
    requires_review = bool(row.get("requires_manual_review") or status != "valid" or new_type == "manual_review")
    row.update({
        "parent_title": title,
        "canonical_title": title,
        "title": title,
        "parent_title_status": status,
        "parent_title_source": recovered["source"],
        "parent_title_quality_issues": title_issues,
        "requires_manual_review": requires_review,
        "quality_grade": "manual_review" if requires_review else row.get("quality_grade", "silver_candidate"),
        "review_status": "pending",
        "title_exclusion_fix_at": now(),
        "title_exclusion_previous_parent_type": old_type,
        "title_exclusion_previous_parent_title": old.get("parent_title", ""),
    })
    if new_type in NO_RAG_PARENT_TYPES or not title:
        row["rag_enabled"] = False
    if decision is None:
        # Classification is frozen for every non-excluded parent.
        row["parent_document_type"] = old_type
        row["content_role"] = old.get("content_role") or role_for(old_type)
    audit = {
        "document_id": row["document_id"], "source_file": row["source_file"],
        "old_parent_document_type": old_type, "new_parent_document_type": row["parent_document_type"],
        "old_title": old.get("parent_title", ""), "new_title": title,
        "old_status": old.get("parent_title_status"), "new_status": status,
        "title_source": recovered["source"], "title_issues": title_issues,
        "classification_evidence": row.get("classification_evidence", []),
        "requires_manual_review": requires_review, "review_status": "pending",
    }
    return row, audit


def adjusted_segment_type(old_type: str, parent_type: str, is_full: bool) -> str:
    if is_full:
        return "full_document"
    if old_type in CONTEXT_SEGMENTS | {"table", "appendix_sds", "embedded_sds", "onsite_disposal_plan", "embedded_special_section"}:
        return old_type
    if parent_type == "sds": return "sds_section"
    if parent_type == "accident_case": return "accident_case_section"
    if parent_type == "regulation": return "regulation_article"
    if parent_type == "guidance_reference": return "guidance_section"
    return old_type


def transform_segment(old: dict[str, Any], parent: dict[str, Any], parent_type_changed: bool) -> tuple[dict[str, Any], dict[str, Any] | None]:
    row = dict(old)
    audit = audit_section_title(row)
    old_title = row.get("section_title") or None
    new_title = audit["section_title"]
    is_full = bool(row.get("is_parent_full_document")) or row.get("segment_type") == "full_document"
    segment_type = adjusted_segment_type(str(row.get("segment_type") or ""), parent["parent_document_type"], is_full) if parent_type_changed else str(row.get("segment_type") or "")
    content_role = role_for(parent["parent_document_type"], segment_type, str(parent.get("content_role") or ""))
    warnings = list(dict.fromkeys([*(row.get("warnings") or []), *(audit.get("issues") or [])]))
    display = f"{parent['parent_title']}｜{new_title}" if new_title else parent["parent_title"]
    row.update({
        "parent_document_type": parent["parent_document_type"],
        "segment_type": segment_type,
        "parent_title": parent["parent_title"],
        "parent_title_status": parent["parent_title_status"],
        "section_title": new_title,
        "section_title_source": audit["source"],
        "section_title_confidence": audit["confidence"],
        "section_title_valid": audit["valid"],
        "display_title": display,
        "title_status": "valid" if new_title else "not_applicable",
        "content_role": content_role,
        "warnings": warnings,
        "requires_manual_review": bool(parent["requires_manual_review"] or (old_title and not audit["valid"])),
        "quality_grade": "manual_review" if parent["requires_manual_review"] else row.get("quality_grade", "silver_candidate"),
        "review_status": "pending",
        "title_exclusion_fix_at": now(),
    })
    if parent["parent_document_type"] in {"sds", "regulation"}:
        row["accident_category"] = "not_applicable"
        row["secondary_topic"] = ""
    change = None
    if old_title != new_title:
        change = {
            "document_id": row["document_id"], "parent_document_id": row["parent_document_id"],
            "source_file": row["source_file"], "segment_type": segment_type,
            "old_section_title": old_title, "new_section_title": new_title,
            "section_title_source": audit["source"], "section_title_confidence": audit["confidence"],
            "issues": audit.get("issues", []), "review_status": "pending",
        }
    return row, change


def assign_rag_eligibility(segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]], changed_parent_types: set[str]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in segments:
        grouped[row["parent_document_id"]].append(row)
    for parent_id, rows in grouped.items():
        parent = parents[parent_id]
        structured = [
            row for row in rows if row.get("is_canonical") and row.get("segment_type") not in CONTEXT_SEGMENTS | {"full_document"}
            and row.get("source_origin") != "source_record_repaired" and len(str(row.get("content") or "")) >= 80
        ]
        for row in rows:
            if parent_id in changed_parent_types:
                enabled = bool(
                    parent.get("rag_enabled") and parent["parent_document_type"] not in NO_RAG_PARENT_TYPES
                    and row.get("is_canonical") and row.get("segment_type") not in CONTEXT_SEGMENTS
                    and row.get("source_origin") != "source_record_repaired"
                    and len(str(row.get("content") or "").strip()) >= 50
                )
            else:
                enabled = bool(row.get("rag_eligible") or row.get("rag_enabled"))
                enabled = enabled and bool(parent.get("rag_enabled")) and bool(parent.get("parent_title"))
            if row.get("segment_type") == "full_document" and structured:
                enabled = False
            if SCRIPT_RE.search(str(row.get("content") or "")) or HTML_TAG_RE.search(str(row.get("content") or "")):
                enabled = False
            row["rag_eligible"] = enabled
            row["rag_enabled"] = enabled


def make_chunks(old_chunks: list[dict[str, Any]], segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]], affected: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    segment_map = {row["canonical_segment_id"]: row for row in segments if row.get("is_canonical")}
    retained: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for old in old_chunks:
        if old["parent_document_id"] in affected:
            superseded.append({**old, "title_exclusion_fix_status": "superseded_by_title_and_exclusion_fix", "review_status": "pending"})
            continue
        segment = segment_map.get(old["canonical_segment_id"])
        parent = parents[old["parent_document_id"]]
        if not segment:
            superseded.append({**old, "title_exclusion_fix_status": "superseded_missing_canonical_segment", "review_status": "pending"})
            continue
        row = {
            **old,
            "parent_title": parent["parent_title"],
            "section_title": segment.get("section_title"),
            "section_title_source": segment["section_title_source"],
            "section_title_confidence": segment["section_title_confidence"],
            "section_title_valid": segment["section_title_valid"],
            "display_title": segment["display_title"],
            "review_status": "pending",
            "title_exclusion_fix_status": "retained_unaffected",
        }
        retained.append(row)
        seen.add(row["normalized_content_hash"])

    generated: list[dict[str, Any]] = []
    exact_removed = 0
    for segment in segments:
        if segment["parent_document_id"] not in affected or not segment.get("rag_eligible") or not segment.get("is_canonical"):
            continue
        parent = parents[segment["parent_document_id"]]
        if not parent["parent_title"]:
            continue
        for order, (local_start, local_end, content) in enumerate(split_for_chunks(str(segment.get("content") or ""), max_chars=1800, overlap=100)):
            content = content.strip()
            if len(content) < 50 or SCRIPT_RE.search(content) or HTML_TAG_RE.search(content):
                continue
            digest = text_hash(content)
            if digest in seen:
                exact_removed += 1
                continue
            seen.add(digest)
            generated.append({
                "record_level": "rag_chunk",
                "chunk_id": stable_id("titlechunk", segment["document_id"], str(order), digest),
                "canonical_segment_id": segment["canonical_segment_id"],
                "parent_document_id": parent["document_id"],
                "parent_title": parent["parent_title"],
                "section_title": segment.get("section_title"),
                "section_title_source": segment["section_title_source"],
                "section_title_confidence": segment["section_title_confidence"],
                "section_title_valid": segment["section_title_valid"],
                "display_title": segment["display_title"],
                "content": content,
                "parent_document_type": parent["parent_document_type"],
                "segment_type": segment["segment_type"],
                "accident_category": segment.get("accident_category", "other"),
                "secondary_topic": segment.get("secondary_topic", ""),
                "content_role": segment["content_role"],
                "chemical_name": parent.get("chemical_name", ""),
                "source_file": parent["source_file"], "relative_path": parent["relative_path"],
                "source_url": parent.get("source_url", ""),
                "source_page_start": segment.get("source_page_start"), "source_page_end": segment.get("source_page_end"),
                "char_start": int(segment.get("char_start") or 0) + local_start,
                "char_end": int(segment.get("char_start") or 0) + local_end,
                "normalized_content_hash": digest,
                "quality_grade": segment.get("quality_grade", "manual_review"),
                "review_status": "pending", "warnings": segment.get("warnings", []),
                "title_exclusion_fix_status": "rebuilt_affected_parent",
            })
    return retained + generated, superseded, exact_removed


def validate_records(parents: list[dict[str, Any]], segments: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    checked = Counter()
    for name, model, rows in (
        ("TitleFixedParent", TitleFixedParent, parents),
        ("TitleFixedSegment", TitleFixedSegment, segments),
        ("TitleFixedChunk", TitleFixedChunk, chunks),
    ):
        for row in rows:
            checked[name] += 1
            try:
                model.model_validate(row)
            except ValidationError as exc:
                failures.append({"model": name, "record_id": row.get("document_id") or row.get("chunk_id"), "errors": exc.errors(include_url=False)})
    return {"checked": dict(checked), "failure_count": len(failures), "failures": failures[:500]}


def rows_audit(path: Path, title: str, rows: list[dict[str, Any]], fields: list[str], preview_field: str = "") -> None:
    sections = [f"生成时间：{now()}\n\n记录数：{len(rows)}\n\n所有记录仍为 `pending`；自动结果不等于人工批准。"]
    for index, row in enumerate(rows, 1):
        body = "\n".join(f"- {field}：{row.get(field, '')}" for field in fields)
        if preview_field:
            body += f"\n- 内容预览：{clean_preview(str(row.get(preview_field) or ''), 700)}"
        sections.append(f"## {index}. {row.get('new_title') or row.get('parent_title') or row.get('display_title') or row.get('document_id')}\n\n{body}\n\n- 人工结论：________")
    write_md(path, title, sections)


def counter_md(counter: Counter[str]) -> str:
    return "\n".join(f"- `{key}`：{value}" for key, value in sorted(counter.items())) or "- 无"


def run(root: Path) -> dict[str, Any]:
    old_cleaned = root / "cleaned_full_v1_semantic_fixed"
    cleaned = root / "cleaned_full_v1_title_fixed"
    reports = root / "reports_full_v1_title_fixed"
    cleaned.mkdir(exist_ok=False)
    reports.mkdir(exist_ok=False)

    old_parents = load_jsonl(old_cleaned / "parent_documents.jsonl")
    old_segments = load_jsonl(old_cleaned / "segment_records.jsonl")
    old_chunks = load_jsonl(old_cleaned / "rag_chunks.jsonl")
    manifests = load_jsonl(root / "manifests_full_v1" / "raw_file_manifest.jsonl")
    old_parent_map = {row["document_id"]: row for row in old_parents}

    parents: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    parent_audits: list[dict[str, Any]] = []
    checkpoint_sources: dict[str, str] = {}
    for old in old_parents:
        checkpoint, checkpoint_source = title_checkpoint(root, old["document_id"])
        text = parsed_text(checkpoint)
        row, audit = transform_parent(old, text, checkpoint)
        parents.append(row); texts[row["document_id"]] = text; parent_audits.append(audit)
        checkpoint_sources[row["document_id"]] = checkpoint_source
    parent_map = {row["document_id"]: row for row in parents}

    parent_type_changes = [row for row in parent_audits if row["old_parent_document_type"] != row["new_parent_document_type"]]
    parent_title_changes = [row for row in parent_audits if row["old_title"] != row["new_title"] or row["old_status"] != row["new_status"]]
    affected = {row["document_id"] for row in [*parent_type_changes, *parent_title_changes]}
    changed_parent_types = {row["document_id"] for row in parent_type_changes}
    # Parents with unresolved/invalid titles must not retain pre-fix chunks,
    # even when the old run happened to have the same missing title state.
    affected.update(row["document_id"] for row in parents if not row["parent_title"] or row["parent_title_quality_issues"])

    segments: list[dict[str, Any]] = []
    section_changes: list[dict[str, Any]] = []
    for old in old_segments:
        parent = parent_map[old["parent_document_id"]]
        row, change = transform_segment(old, parent, parent["document_id"] in changed_parent_types)
        segments.append(row)
        if change:
            section_changes.append(change)
            affected.add(parent["document_id"])
        if row.get("display_title") != old.get("display_title"):
            affected.add(parent["document_id"])

    assign_rag_eligibility(segments, parent_map, changed_parent_types)
    chunks, superseded, exact_removed = make_chunks(old_chunks, segments, parent_map, affected)
    canonical = [row for row in segments if row.get("is_canonical")]
    old_relations = load_jsonl(old_cleaned / "duplicate_segment_relations.jsonl")

    validation = validate_records(parents, segments, chunks)
    _, actual_hashes, read_failures = raw_content_inventory(root / "raw", manifests)
    raw_ok, raw_detail = raw_integrity_by_content(manifests, actual_hashes, read_failures)

    dump_jsonl(cleaned / "parent_documents.jsonl", parents)
    dump_jsonl(cleaned / "segment_records.jsonl", segments)
    dump_jsonl(cleaned / "canonical_segments.jsonl", canonical)
    dump_jsonl(cleaned / "duplicate_segment_relations.jsonl", old_relations)
    dump_jsonl(cleaned / "rag_chunks.jsonl", chunks)
    dump_jsonl(cleaned / "superseded_chunks.jsonl", superseded)
    dump_json(cleaned / "affected_parent_ids.json", sorted(affected))
    dump_json(cleaned / "title_fix_validation.json", validation)
    dump_json(cleaned / "raw_integrity_check.json", raw_detail)
    dump_json(cleaned / "checkpoint_sources.json", checkpoint_sources)
    for filename, ptype in (
        ("sds_records.jsonl", "sds"), ("enterprise_plans.jsonl", "enterprise_plan"),
        ("enterprise_group_plans.jsonl", "enterprise_group_plan"), ("accident_cases.jsonl", "accident_case"),
        ("regulations.jsonl", "regulation"), ("guidance_references.jsonl", "guidance_reference"),
        ("excluded_irrelevant.jsonl", "excluded_irrelevant"), ("manual_review.jsonl", "manual_review"),
    ):
        dump_jsonl(cleaned / filename, (row for row in parents if row["parent_document_type"] == ptype))

    old_excluded_ids = {row["document_id"] for row in old_parents if row["parent_document_type"] == "excluded_irrelevant"}
    excluded_recheck = [next(item for item in parent_audits if item["document_id"] == document_id) for document_id in old_excluded_ids]
    restored = [row for row in excluded_recheck if row["new_parent_document_type"] != "excluded_irrelevant"]
    sds_rows = [row for row in parents if row["parent_document_type"] == "sds"]
    enterprise_rows = [row for row in parents if row["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"}]
    invalid_parent_rows = [row for row in parents if row["parent_title_quality_issues"]]
    invalid_section_rows = [row for row in segments if not row["section_title_valid"] and (row.get("section_title") or any(change["document_id"] == row["document_id"] for change in section_changes))]
    affected_chunks = [row for row in chunks if row["parent_document_id"] in affected]

    rows_audit(reports / "all_sds_title_review.md", "全部SDS父标题审核包", sds_rows, ["document_id", "source_file", "parent_title", "parent_title_status", "parent_title_source", "chemical_name", "parent_title_quality_issues"])
    rows_audit(reports / "all_enterprise_title_review.md", "全部企业与企业集团预案父标题审核包", enterprise_rows, ["document_id", "parent_document_type", "source_file", "parent_title", "parent_title_status", "parent_title_source", "parent_title_quality_issues", "requires_manual_review"])
    rows_audit(reports / "all_excluded_irrelevant_recheck.md", "原45条excluded_irrelevant逐条复核", excluded_recheck, ["document_id", "source_file", "old_parent_document_type", "new_parent_document_type", "old_title", "new_title", "classification_evidence", "requires_manual_review"])
    rows_audit(reports / "restored_from_excluded_report.md", "从excluded_irrelevant恢复的记录", restored, ["document_id", "source_file", "new_parent_document_type", "new_title", "classification_evidence", "requires_manual_review"])
    rows_audit(reports / "parent_title_quality_audit.md", "父标题质量审计", parent_audits, ["document_id", "source_file", "new_parent_document_type", "old_title", "new_title", "new_status", "title_source", "title_issues", "requires_manual_review"])
    rows_audit(reports / "section_title_quality_audit.md", "章节标题质量审计", section_changes, ["document_id", "parent_document_id", "source_file", "segment_type", "old_section_title", "new_section_title", "section_title_source", "section_title_confidence", "issues"])
    rows_audit(reports / "changed_parent_titles.md", "父标题修复前后变化", parent_title_changes, ["document_id", "source_file", "old_parent_document_type", "new_parent_document_type", "old_title", "new_title", "old_status", "new_status", "title_source", "title_issues"])
    rows_audit(reports / "changed_section_titles.md", "章节标题修复前后变化", section_changes, ["document_id", "parent_document_id", "source_file", "segment_type", "old_section_title", "new_section_title", "section_title_source", "issues"])
    rows_audit(reports / "affected_rag_chunks_review.md", "受影响RAG Chunk审核包", affected_chunks, ["chunk_id", "parent_document_id", "parent_document_type", "segment_type", "parent_title", "section_title", "section_title_source", "section_title_confidence", "section_title_valid", "display_title", "source_file", "char_start", "char_end"], "content")

    review_package(reports / "parent_sample_100.md", "父文档分层抽查100条", stratified_sample(parents, "parent_document_type", 100, SEED), "parent")
    review_package(reports / "segment_sample_100.md", "Segment分层抽查100条", stratified_sample(canonical, "segment_type", 100, SEED), "segment")
    review_package(reports / "rag_chunk_sample_200.md", "RAG Chunk分层抽查200条", stratified_sample(chunks, "parent_document_type", 200, SEED), "chunk")

    parent_issue_counts = Counter(issue for row in parents for issue in row["parent_title_quality_issues"])
    section_issue_counts = Counter(issue for row in section_changes for issue in row["issues"])
    parent_counts = Counter(row["parent_document_type"] for row in parents)
    segment_counts = Counter(row["segment_type"] for row in segments)
    chunk_counts = Counter(row["parent_document_type"] for row in chunks)

    segment_map = {row["canonical_segment_id"]: row for row in canonical}
    type_inconsistent = sum(
        parent_map[row["parent_document_id"]]["parent_document_type"] != row["parent_document_type"]
        for row in segments
    ) + sum(
        parent_map[row["parent_document_id"]]["parent_document_type"] != row["parent_document_type"]
        or segment_map.get(row["canonical_segment_id"], {}).get("parent_document_type") != row["parent_document_type"]
        for row in chunks
    )
    bad_sds_titles = [row for row in sds_rows if FIELD_STICK_RE.search(row["parent_title"])]
    bad_enterprise = [row for row in enterprise_rows if parent_title_issues(row["parent_title"], row["parent_document_type"])]
    strong_excluded = [row for row in parents if row["parent_document_type"] == "excluded_irrelevant" and strong_sds_evidence(texts[row["document_id"]], row["parent_title"], row["source_file"])["matched"]]
    accident_excluded = [row for row in parents if row["parent_document_type"] == "excluded_irrelevant" and re.search(r"事故典型案例|事故调查报告|事故评估报告", f"{row['parent_title']}\n{Path(row['source_file']).stem}")]
    failed_excluded = [row for row in parents if row["parent_document_type"] == "excluded_irrelevant" and row.get("parse_status") != "success"]
    bad_section_live = [row for row in segments if row.get("section_title") and (SECTION_DATE_ONLY_RE.fullmatch(row["section_title"]) or SECTION_DATE_SENTENCE_RE.search(row["section_title"]) or PERSON_ROLE_RE.search(row["section_title"]))]
    bad_chunk_titles = [row for row in chunks if parent_title_issues(row["parent_title"], row["parent_document_type"])]
    html_navigation_chunks = [row for row in chunks if has_navigation_or_script_pollution(row["content"])]

    # Tests inspect the generated output, so run them after persistence.
    pytest_result = run_pytest(root)
    acceptance = {
        "1_sds_no_english_name_label": not any("化学品英文名称" in row["parent_title"] for row in sds_rows),
        "2_sds_no_identity_section_label": not any("化学品及企业标识" in row["parent_title"] for row in sds_rows),
        "3_sds_no_gbt_basis": not any(re.search(r"依据\s*GB/T", row["parent_title"], re.I) for row in sds_rows),
        "4_sds_no_page_prefix": not any(re.match(r"^\s*\d+\s*/\s*\d+", row["parent_title"]) for row in sds_rows),
        "5_sds_no_field_stickiness": not bad_sds_titles,
        "6_enterprise_no_numeric_title": not any(re.fullmatch(r"\d+", row["parent_title"]) for row in enterprise_rows),
        "7_enterprise_no_filename_title": not bad_enterprise,
        "8_enterprise_no_version_title": not any(row["parent_title"] == "应急预案版本号" for row in enterprise_rows),
        "9_no_clear_sds_excluded": not strong_excluded,
        "10_no_clear_accident_excluded": not accident_excluded,
        "11_parse_failure_not_excluded": not failed_excluded,
        "12_no_body_sentence_parent_title": not any("body_sentence" in row["parent_title_quality_issues"] for row in parents),
        "13_no_date_or_personnel_section_title": not bad_section_live,
        "14_unverified_section_title_is_null": all(bool(row.get("section_title")) == bool(row.get("section_title_valid")) for row in segments),
        "15_all_chunk_parent_titles_pass": not bad_chunk_titles,
        "16_parent_segment_chunk_type_consistent": type_inconsistent == 0,
        "17_no_empty_chunks": not any(not str(row.get("content") or "").strip() for row in chunks),
        "18_no_javascript_navigation_chunks": not html_navigation_chunks,
        "19_pydantic_all_pass": validation["failure_count"] == 0,
        "20_pytest_all_pass": pytest_result["passed"],
        "21_all_pending": all(row.get("review_status") == "pending" for row in [*parents, *segments, *chunks]),
        "22_raw_unchanged": raw_ok,
        "23_no_embedding": not any(cleaned.glob("*embedding*")),
        "24_no_database_import": True,
    }
    passed = all(acceptance.values())
    dump_json(cleaned / "acceptance_checks.json", {"passed": passed, "checks": acceptance})
    dump_json(cleaned / "pytest_result.json", pytest_result)
    dump_json(cleaned / "title_fix_metrics.json", {
        "parent_counts": parent_counts, "segment_counts": segment_counts, "chunk_counts": chunk_counts,
        "affected_parents": len(affected), "parent_type_changes": len(parent_type_changes),
        "parent_title_changes": len(parent_title_changes), "section_title_changes": len(section_changes),
        "old_chunks_superseded": len(superseded), "chunks_exact_duplicate_removed": exact_removed,
        "parent_title_issue_counts": parent_issue_counts, "section_title_issue_counts": section_issue_counts,
    })

    write_md(reports / "final_title_and_exclusion_fix_report.md", "全量数据入库前最终标题与误排修复报告", [
        f"自动验收结论：**{'通过' if passed else '未通过'}**。本结论不代表人工专业审核完成。",
        f"- 父文档：{len(parents)}\n- Segment：{len(segments)}\n- canonical Segment：{len(canonical)}\n- RAG Chunk：{len(chunks)}",
        f"- 原excluded_irrelevant复核：{len(excluded_recheck)}\n- 从excluded恢复：{len(restored)}\n- 父标题变化：{len(parent_title_changes)}\n- 章节标题变化：{len(section_changes)}\n- 受影响父文档：{len(affected)}",
        f"- 仅受影响旧Chunk标记废弃：{len(superseded)}\n- 当前受影响Chunk：{len(affected_chunks)}\n- 精确重复Chunk排除：{exact_removed}",
        "## 父类型分布\n\n" + counter_md(parent_counts),
        "## Segment类型分布\n\n" + counter_md(segment_counts),
        "## 父标题问题统计\n\n" + counter_md(parent_issue_counts),
        "## 章节标题修复原因\n\n" + counter_md(section_issue_counts),
        f"- Pydantic失败：{validation['failure_count']}\n- pytest通过：{pytest_result['passed']}\n- raw SHA-256未变化：{raw_ok}",
        "## 24项验收\n\n" + "\n".join(f"- [{'x' if ok else ' '}] {name}" for name, ok in acceptance.items()),
        "未修改 raw，未生成 Embedding，未导入数据库或知识库；所有记录仍为 pending。",
    ])
    write_md(reports / "regression_test_report.md", "回归测试报告", [f"- returncode：{pytest_result['returncode']}\n- 全部通过：{pytest_result['passed']}\n\n```text\n{pytest_result['output']}\n```"])
    return {
        "passed": passed, "acceptance": acceptance, "pytest": pytest_result,
        "validation": validation, "counts": {"parents": len(parents), "segments": len(segments), "canonical_segments": len(canonical), "chunks": len(chunks), "affected_parents": len(affected), "restored_from_excluded": len(restored)},
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=ROOT)
    return value


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(run(args.root.resolve()), ensure_ascii=False, indent=2, default=list))


if __name__ == "__main__":
    main()
