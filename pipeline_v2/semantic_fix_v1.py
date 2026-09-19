"""Final semantic-classification and RAG-content repair.

The runner consumes existing parse checkpoints.  It only re-cleans local HTML
bytes, propagates changed semantics through the three record layers, and
incrementally rebuilds chunks for affected parents.  It never writes under
``raw`` and has no network, embedding, or database integration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .full_runner_v1 import split_for_chunks, stable_id
from .repair_full_v1 import (
    canonicalize_segments,
    clean_preview,
    dump_json,
    dump_jsonl,
    load_jsonl,
    now,
    raw_unchanged,
    review_package,
    run_pytest,
    split_structural_sections,
    stratified_sample,
    validate_all,
    write_md,
)
from .repair_rules_v1 import accident_category_repaired, text_hash
from .semantic_rules_v1 import (
    ACTION_TITLE_RE,
    ARTICLE_TITLE_RE,
    HTML_TAG_RE,
    NAV_WORD_RE,
    PAGE_RE,
    SCRIPT_RE,
    accident_evidence,
    classify_parent_semantic,
    classify_segment_semantic,
    clean_html_semantic,
    has_navigation_or_script_pollution,
    is_pubchem,
    invalid_parent_title,
    recover_parent_title_semantic,
    segment_rag_enabled,
    strong_sds_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
SEED = 20260824
HTML_SUFFIXES = {".html", ".htm"}
EXCLUDED_SEGMENT_TYPES = {
    "front_matter", "metadata_section", "personnel_table", "contact_list",
    "toc_fragment", "reference_sentence", "list_fragment",
}


def parsed_checkpoint(root: Path, document_id: str) -> tuple[dict[str, Any], str]:
    candidates = (
        root / "checkpoints_full_v1_repaired" / "parsed_documents" / f"{document_id}.json",
        root / "checkpoints_full_v1" / "parsed_documents" / f"{document_id}.json",
    )
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), str(path)
    return {"parse_status": "failed", "raw_text": "", "normalized_text": "", "warnings": ["parsed_checkpoint_missing"]}, ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def raw_content_inventory(raw_dir: Path, manifests: list[dict[str, Any]]) -> tuple[dict[str, list[Path]], Counter[str], list[dict[str, str]]]:
    """Hash only files covered by the SHA manifest plus path-alias candidates.

    Files lacking an original SHA can include unavailable cloud placeholders;
    reading those is outside the established integrity-check scope.
    """
    manifest_by_path = {Path(row["source_file"]).resolve(): row for row in manifests}
    expected_paths = set(manifest_by_path)
    by_hash: dict[str, list[Path]] = defaultdict(list)
    hashes: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    for path in sorted(item for item in raw_dir.rglob("*") if item.is_file()):
        resolved = path.resolve()
        manifest = manifest_by_path.get(resolved)
        if path.name.lower() in {".ds_store", "thumbs.db", "desktop.ini"}:
            continue
        # Exact-path records without a baseline SHA are not assertable and are
        # intentionally skipped, matching the established repaired-run check.
        if manifest is not None and not manifest.get("sha256"):
            continue
        # An unexpected current path may be a filesystem-truncated alias and
        # must be hashed so it can be matched to a missing manifest path.
        if manifest is None and resolved in expected_paths:
            continue
        try:
            digest = sha256_file(path)
        except Exception as exc:
            failures.append({"source_file": str(resolved), "reason": f"hash_read_failed:{type(exc).__name__}:{str(exc)[:120]}"})
            continue
        by_hash[digest].append(resolved)
        hashes[digest] += 1
    return by_hash, hashes, failures


def resolve_real_source_path(row: dict[str, Any], by_hash: dict[str, list[Path]], raw_dir: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source = Path(row["source_file"])
    if source.exists():
        return row, None
    matches = by_hash.get(str(row.get("sha256") or ""), [])
    if not matches:
        return row, None
    # Identical duplicates are still content-equivalent.  Prefer the path
    # sharing the manifest's parent directory, then use deterministic order.
    same_parent = [path for path in matches if path.parent == source.parent]
    actual = sorted(same_parent or matches)[0]
    fixed = {**row, "source_file": str(actual), "relative_path": str(actual.relative_to(raw_dir))}
    return fixed, {
        "document_id": row["document_id"], "manifest_source_file": str(source),
        "resolved_source_file": str(actual), "sha256": row.get("sha256"),
        "reason": "manifest_filename_exceeds_or_differs_from_actual_filesystem_name",
        "review_status": "pending",
    }


def raw_integrity_by_content(manifests: list[dict[str, Any]], actual_hashes: Counter[str], read_failures: list[dict[str, str]] | None = None) -> tuple[bool, dict[str, Any]]:
    expected = Counter(str(row.get("sha256")) for row in manifests if row.get("sha256") and Path(row["source_file"]).name.lower() not in {".ds_store", "thumbs.db", "desktop.ini"})
    missing = expected - actual_hashes
    added = actual_hashes - expected
    read_failures = read_failures or []
    detail = {
        "method": "sha256_multiset_comparison",
        "expected_hashed_files": sum(expected.values()), "actual_hashed_files": sum(actual_hashes.values()),
        "missing_hash_counts": dict(missing), "unexpected_hash_counts": dict(added),
        "read_failures": read_failures,
        "unchanged": not missing and not added and not read_failures,
    }
    return detail["unchanged"], detail


def semantic_html_checkpoint(parent: dict[str, Any], checkpoint_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(parent["source_file"])
    try:
        result = clean_html_semantic(source.read_bytes())
        result.update({
            "parser_version": "semantic-html-v1",
            "document_id": parent["document_id"],
            "source_file": str(source),
            "review_status": "pending",
            "processed_at": now(),
            "warnings": [] if result["parse_status"] == "success" else ["html_content_extraction_failed"],
        })
    except Exception as exc:
        result = {
            "parser": "html_semantic_cleaner", "parser_version": "semantic-html-v1",
            "document_id": parent["document_id"], "source_file": str(source),
            "title": "", "h1": "", "raw_text": "", "normalized_text": "",
            "text_length": 0, "parse_status": "html_content_extraction_failed",
            "html_page_type": "read_error", "javascript_hits": 0,
            "html_tag_hits": 0, "navigation_hits": 0,
            "removed_script_lines": 0, "removed_navigation_lines": 0,
            "review_status": "pending", "processed_at": now(),
            "warnings": [f"html_local_read_failed:{type(exc).__name__}:{str(exc)[:180]}"],
        }
    dump_json(checkpoint_dir / "parsed_documents" / f"{parent['document_id']}.json", result)
    audit = {key: result.get(key) for key in (
        "document_id", "source_file", "parser", "encoding", "parse_status",
        "html_page_type", "text_length", "javascript_hits", "html_tag_hits",
        "navigation_hits", "removed_script_lines", "removed_navigation_lines", "warnings",
    )}
    return result, audit


def content_role(parent_type: str, segment_type: str | None = None) -> str:
    if parent_type == "chemical_reference": return "chemical_reference"
    if parent_type == "excluded_irrelevant": return "excluded_irrelevant"
    if segment_type in {"front_matter", "metadata_section", "personnel_table", "contact_list", "toc_fragment", "reference_sentence", "list_fragment"}: return "context_only"
    if segment_type in {"sds_section", "appendix_sds", "embedded_sds"} or parent_type == "sds": return "safety_data_section" if segment_type else "safety_data"
    if segment_type == "regulation_article" or parent_type == "regulation": return "regulatory_article" if segment_type else "regulatory_reference"
    if segment_type == "accident_case_section" or parent_type == "accident_case": return "accident_case_reference"
    if parent_type == "guidance_reference" or segment_type == "guidance_section": return "guidance_reference"
    if parent_type == "chemical_catalog": return "chemical_catalog"
    if parent_type == "manual_review": return "manual_review"
    return "emergency_plan"


def category(parent_type: str, title: str, text: str) -> tuple[str, str]:
    if parent_type in {"sds", "regulation", "chemical_reference", "chemical_catalog", "excluded_irrelevant"}:
        return "not_applicable", ""
    return accident_category_repaired(title, text, parent_type)


def make_parent(old: dict[str, Any], parsed: dict[str, Any], html_meta: dict[str, Any] | None) -> dict[str, Any]:
    text = str(parsed.get("normalized_text") or parsed.get("raw_text") or "")
    candidate = dict(old)
    candidate["parse_status"] = str(parsed.get("parse_status") or old.get("parse_status") or "failed")
    decision = classify_parent_semantic(candidate, text)
    new_type = decision["parent_document_type"]
    provisional = {**candidate, "parent_document_type": new_type}
    recovered = recover_parent_title_semantic(provisional, text, html_meta)
    title = recovered["title"]
    accident, secondary = category(new_type, title, text)
    parse_status = candidate["parse_status"]
    rag_enabled = bool(decision.get("rag_enabled", True)) and parse_status == "success" and bool(title)
    requires_review = bool(
        decision.get("requires_manual_review") or recovered["status"] != "valid" or
        parse_status != "success" or new_type == "manual_review"
    )
    warnings = list(dict.fromkeys([
        *(old.get("warnings") or []), *(parsed.get("warnings") or []),
        *(recovered.get("issues") or []),
    ]))
    if parse_status == "html_content_extraction_failed":
        warnings.append("html_content_extraction_failed")
    row = {
        **old,
        "record_level": "parent_document",
        "parent_document_type": new_type,
        "parent_type_locked": True,
        "parent_type_evidence_scope": "semantic_document_structure_v1",
        "classification_evidence": decision.get("evidence", []),
        "classification_confidence": decision.get("confidence", 0.0),
        "classification_reasons": [f"semantic_fix_from_{old.get('parent_document_type')}_to_{new_type}"],
        "semantic_fix_previous_parent_type": old.get("parent_document_type"),
        "semantic_fix_previous_parent_title": old.get("parent_title", ""),
        "classification_conflict": new_type == "manual_review",
        "parent_title": title,
        "parent_title_status": recovered["status"],
        "parent_title_source": recovered["source"],
        "canonical_title": title,
        "title": title,
        "parse_status": parse_status,
        "parser_selected": parsed.get("parser") or old.get("parser_selected", ""),
        "parser_version": parsed.get("parser_version") or old.get("parser_version", ""),
        "text_length": len(text),
        "accident_category": accident,
        "secondary_topic": secondary,
        "content_role": decision.get("content_role") or content_role(new_type),
        "rag_enabled": rag_enabled,
        "requires_manual_review": requires_review,
        "quality_grade": "manual_review" if requires_review else "gold_candidate" if decision.get("confidence", 0) >= 0.88 else "silver_candidate",
        "warnings": warnings,
        "review_status": "pending",
        "semantic_fix_at": now(),
        "_text": text,
        "_html_meta": html_meta or {},
    }
    return row


def segment_from_text(parent: dict[str, Any], text: str, title: str, start: int, end: int, origin: str, suggested_type: str, full: bool = False) -> dict[str, Any]:
    segment_id = stable_id("semseg", parent["document_id"], origin, str(start), title, text_hash(text))
    temp = {"content": text, "section_title": title, "segment_type": suggested_type, "is_parent_full_document": full}
    segment_type, warnings = classify_segment_semantic(temp, parent["parent_document_type"])
    if full: segment_type = "full_document"
    accident, secondary = category(parent["parent_document_type"], title or parent["parent_title"], text)
    display = f"{parent['parent_title']}｜{title}" if title else parent["parent_title"]
    return {
        "record_level": "segment_record", "document_id": segment_id,
        "parent_document_id": parent["document_id"], "canonical_segment_id": segment_id,
        "is_canonical": True, "duplicate_segment_ids": [], "duplicate_reason": "",
        "normalized_content_hash": text_hash(text), "parent_document_type": parent["parent_document_type"],
        "segment_type": segment_type, "parent_title": parent["parent_title"],
        "parent_title_status": parent["parent_title_status"], "section_title": title,
        "display_title": display, "title_status": "valid" if title else "not_applicable",
        "content": text, "raw_text": text, "accident_category": accident,
        "secondary_topic": secondary, "content_role": content_role(parent["parent_document_type"], segment_type),
        "source_file": parent["source_file"], "relative_path": parent["relative_path"],
        "source_url": parent.get("source_url", ""), "source_page_start": None, "source_page_end": None,
        "char_start": max(0, start), "char_end": max(start, end),
        "segment_start": max(0, start), "segment_end": max(start, end),
        "segment_classification_text_source": "semantic_html_text" if Path(parent["source_file"]).suffix.lower() in HTML_SUFFIXES else "raw_text",
        "segment_local_text_length": len(text), "parent_context_used": False,
        "segment_classification_audit": {"semantic_fix": True}, "source_origin": origin,
        "is_parent_full_document": full, "warnings": warnings,
        "quality_grade": parent["quality_grade"], "review_status": "pending",
        "requires_manual_review": parent["requires_manual_review"], "rag_eligible": False,
    }


def rebuild_html_segments(parent: dict[str, Any]) -> list[dict[str, Any]]:
    text = parent["_text"]
    if parent["parse_status"] != "success" or not text:
        return []
    rows = [segment_from_text(parent, text, "", 0, len(text), "semantic_html_full", "full_document", True)]
    for order, (heading, content, start, end, stype) in enumerate(split_structural_sections(text, parent["parent_document_type"])):
        rows.append(segment_from_text(parent, content, heading, start, end, f"semantic_html_section_{order}", stype))
    rows, _ = canonicalize_segments(rows)
    return rows


def transform_segment(old: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    row = dict(old)
    text = str(row.get("content") or row.get("raw_text") or "")
    new_type, semantic_warnings = classify_segment_semantic(row, parent["parent_document_type"])
    if row.get("is_parent_full_document"):
        new_type = "full_document"
    accident, secondary = category(parent["parent_document_type"], row.get("section_title") or parent["parent_title"], text)
    section_title = str(row.get("section_title") or "")
    row.update({
        "parent_document_type": parent["parent_document_type"], "segment_type": new_type,
        "parent_title": parent["parent_title"], "parent_title_status": parent["parent_title_status"],
        "display_title": f"{parent['parent_title']}｜{section_title}" if section_title else parent["parent_title"],
        "accident_category": accident, "secondary_topic": secondary,
        "content_role": content_role(parent["parent_document_type"], new_type),
        "warnings": list(dict.fromkeys([*(row.get("warnings") or []), *semantic_warnings])),
        "quality_grade": parent["quality_grade"], "requires_manual_review": parent["requires_manual_review"],
        "review_status": "pending", "semantic_fix_at": now(),
    })
    return row


def assign_segment_rag(segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in segments: grouped[row["parent_document_id"]].append(row)
    for parent_id, rows in grouped.items():
        parent = parents[parent_id]
        structured = [row for row in rows if row.get("is_canonical") and row.get("segment_type") not in EXCLUDED_SEGMENT_TYPES | {"full_document"} and row.get("source_origin") not in {"source_record_repaired"} and len(row.get("content", "")) >= 80]
        for row in rows:
            enabled = segment_rag_enabled(row, parent)
            if row["segment_type"] == "full_document" and structured:
                enabled = False
                row["warnings"] = list(dict.fromkeys([*(row.get("warnings") or []), "full_document_superseded_by_structured_segments"]))
            row["rag_eligible"] = enabled
            row["rag_enabled"] = enabled


def make_incremental_chunks(
    old_chunks: list[dict[str, Any]], segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]], affected: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    retained: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for old in old_chunks:
        if old["parent_document_id"] in affected:
            superseded.append({**old, "semantic_fix_status": "superseded_by_semantic_fix", "review_status": "pending"})
        else:
            row = {**old, "semantic_fix_status": "retained_unaffected", "review_status": "pending"}
            retained.append(row)
            seen_hashes.add(row["normalized_content_hash"])
    generated: list[dict[str, Any]] = []
    exact_removed = 0
    for segment in segments:
        if segment["parent_document_id"] not in affected or not segment.get("rag_eligible"):
            continue
        parent = parents[segment["parent_document_id"]]
        for order, (local_start, local_end, content) in enumerate(split_for_chunks(segment["content"], max_chars=1800, overlap=100)):
            content = content.strip()
            if len(content) < 50 or SCRIPT_RE.search(content) or HTML_TAG_RE.search(content):
                continue
            digest = text_hash(content)
            if digest in seen_hashes:
                exact_removed += 1
                continue
            seen_hashes.add(digest)
            row = {
                "record_level": "rag_chunk", "chunk_id": stable_id("semchunk", segment["document_id"], str(order), digest),
                "canonical_segment_id": segment["canonical_segment_id"], "parent_document_id": parent["document_id"],
                "parent_title": parent["parent_title"], "section_title": segment.get("section_title", ""),
                "display_title": segment.get("display_title") or parent["parent_title"], "content": content,
                "parent_document_type": parent["parent_document_type"], "segment_type": segment["segment_type"],
                "accident_category": segment["accident_category"], "secondary_topic": segment["secondary_topic"],
                "content_role": segment["content_role"], "chemical_name": parent.get("chemical_name", ""),
                "source_file": parent["source_file"], "relative_path": parent["relative_path"],
                "source_url": parent.get("source_url", ""), "source_page_start": segment.get("source_page_start"),
                "source_page_end": segment.get("source_page_end"), "char_start": segment["char_start"] + local_start,
                "char_end": segment["char_start"] + local_end, "normalized_content_hash": digest,
                "quality_grade": segment["quality_grade"], "review_status": "pending",
                "warnings": list(segment.get("warnings") or []), "semantic_fix_status": "rebuilt_affected_parent",
            }
            generated.append(row)
    return retained + generated, superseded, exact_removed


def title_risk(title: str) -> list[str]:
    issues = invalid_parent_title(title)
    if ARTICLE_TITLE_RE.match(title): issues.append("legal_article_title")
    if PAGE_RE.match(title) or re.match(r"^\s*\d+\s*/\s*\d+", title): issues.append("page_title")
    if re.fullmatch(r"(?:化学品及企业标识|急救措施|消防措施|泄漏应急处理|操作处置与储存)", title): issues.append("sds_section_title")
    if ACTION_TITLE_RE.match(title) or re.search(r"建议由.{0,80}处罚|造成.{0,30}伤亡|负有.{0,20}责任", title): issues.append("body_sentence_title")
    return sorted(set(issues))


def markdown_count(counter: Counter[str]) -> str:
    return "\n".join(f"- `{key}`：{value}" for key, value in sorted(counter.items())) or "- 无"


def rows_report(path: Path, title: str, rows: list[dict[str, Any]], fields: list[str]) -> None:
    sections = [f"生成时间：{now()}\n\n记录数：{len(rows)}\n\n审核状态均为 `pending`。"]
    for index, row in enumerate(rows, 1):
        details = "\n".join(f"- {field}：{row.get(field, '')}" for field in fields)
        sections.append(f"## {index}. {row.get('parent_title') or row.get('display_title') or row.get('document_id')}\n\n{details}\n\n- 人工结论：________")
    write_md(path, title, sections)


def quality_metrics(parents: list[dict[str, Any]], segments: list[dict[str, Any]], chunks: list[dict[str, Any]], texts: dict[str, str]) -> dict[str, Any]:
    parent_map = {row["document_id"]: row for row in parents}
    strong = []
    for row in parents:
        evidence = strong_sds_evidence(texts.get(row["document_id"], ""), row["parent_title"], row["source_file"])
        # PubChem is a source artifact governed by the explicit unified
        # chemical_reference policy.  It is not an SDS recall target even when
        # its large JSON happens to contain eight SDS-like label names.
        if evidence["matched"] and not is_pubchem(row["source_file"], row["parent_title"], texts.get(row["document_id"], "")):
            strong.append((row, evidence))
    enterprise_false = sum(
        row["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"} and
        bool(re.search(r"编制导则|GB/T\s*29639|事故调查报告|Safety Data Sheet|\bM?SDS\b|人民政府|街道办事处", Path(row["source_file"]).stem, re.I))
        for row in parents
    )
    accident_false = sum(
        row["parent_document_type"] == "accident_case" and
        not accident_evidence(texts.get(row["document_id"], ""), row["parent_title"], row["source_file"])["matched"]
        for row in parents
    )
    title_issues = {row["document_id"]: title_risk(row["parent_title"]) for row in parents}
    js_chunks = [row for row in chunks if has_navigation_or_script_pollution(row["content"])]
    front_chunks = [row for row in chunks if row["segment_type"] in {"front_matter", "metadata_section", "personnel_table", "contact_list"}]
    pubchem_chunks = [row for row in chunks if "pubchem" in row["source_file"].lower() or row["parent_document_type"] == "chemical_reference"]
    parent_segment_inconsistent = sum(parent_map.get(row["parent_document_id"], {}).get("parent_document_type") != row["parent_document_type"] for row in segments)
    segment_map = {row["canonical_segment_id"]: row for row in segments if row.get("is_canonical")}
    chunk_inconsistent = sum(
        parent_map.get(row["parent_document_id"], {}).get("parent_document_type") != row["parent_document_type"] or
        segment_map.get(row["canonical_segment_id"], {}).get("parent_document_type") != row["parent_document_type"]
        for row in chunks
    )
    duplicate_hashes = sum(count - 1 for count in Counter(row["normalized_content_hash"] for row in chunks).values() if count > 1)
    return {
        "strong_sds_total": len(strong),
        "strong_sds_as_sds": sum(row["parent_document_type"] == "sds" for row, _ in strong),
        "sds_structure_recall": round(sum(row["parent_document_type"] == "sds" for row, _ in strong) / max(1, len(strong)), 4),
        "strong_sds_as_guidance": sum(row["parent_document_type"] == "guidance_reference" for row, _ in strong),
        "strong_sds_as_regulation": sum(row["parent_document_type"] == "regulation" for row, _ in strong),
        "strong_sds_as_excluded": sum(row["parent_document_type"] == "excluded_irrelevant" for row, _ in strong),
        "enterprise_false_positive": enterprise_false, "accident_case_false_positive": accident_false,
        "parent_title_body_sentence": sum("body_sentence_title" in issues or "sentence_or_action" in issues for issues in title_issues.values()),
        "parent_title_legal_article": sum("legal_article_title" in issues or "regulation_article_body" in issues for issues in title_issues.values()),
        "parent_title_page_or_sds_section": sum("page_title" in issues or "page_number" in issues or "sds_section_title" in issues or "section_title_as_parent" in issues for issues in title_issues.values()),
        "html_script_navigation_chunks": len(js_chunks), "front_matter_personnel_chunks": len(front_chunks),
        "pubchem_chunks": len(pubchem_chunks), "parent_segment_type_inconsistency": parent_segment_inconsistent,
        "parent_segment_chunk_type_inconsistency": chunk_inconsistent, "empty_chunks": sum(not row["content"].strip() for row in chunks),
        "rec_seg_duplicate_chunks": duplicate_hashes,
    }


def run(root: Path) -> dict[str, Any]:
    old_cleaned = root / "cleaned_full_v1_repaired"
    cleaned = root / "cleaned_full_v1_semantic_fixed"
    reports = root / "reports_full_v1_semantic_fixed"
    checkpoints = root / "checkpoints_full_v1_semantic_fixed"
    cleaned.mkdir(exist_ok=False); reports.mkdir(exist_ok=False); checkpoints.mkdir(exist_ok=False)

    manifests = load_jsonl(root / "manifests_full_v1" / "raw_file_manifest.jsonl")
    old_parents = load_jsonl(old_cleaned / "parent_documents.jsonl")
    old_segments = load_jsonl(old_cleaned / "segment_records.jsonl")
    old_chunks = load_jsonl(old_cleaned / "rag_chunks.jsonl")
    old_parent_map = {row["document_id"]: row for row in old_parents}
    old_segments_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in old_segments: old_segments_by_parent[row["parent_document_id"]].append(row)

    raw_dir = root / "raw"
    actual_by_hash, actual_hashes, raw_read_failures = raw_content_inventory(raw_dir, manifests)
    resolved_old_parents: list[dict[str, Any]] = []
    source_path_aliases: list[dict[str, Any]] = []
    for old in old_parents:
        resolved, alias = resolve_real_source_path(old, actual_by_hash, raw_dir)
        resolved_old_parents.append(resolved)
        if alias: source_path_aliases.append(alias)

    parents: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    html_audits: list[dict[str, Any]] = []
    html_ids: set[str] = set()
    checkpoint_sources: dict[str, str] = {}
    for old in resolved_old_parents:
        if Path(old["source_file"]).suffix.lower() in HTML_SUFFIXES:
            parsed, html_audit = semantic_html_checkpoint(old, checkpoints)
            html_audits.append(html_audit); html_ids.add(old["document_id"])
            html_meta = {"title": parsed.get("title", ""), "h1": parsed.get("h1", "")}
            checkpoint_sources[old["document_id"]] = str(checkpoints / "parsed_documents" / f"{old['document_id']}.json")
        else:
            parsed, checkpoint_source = parsed_checkpoint(root, old["document_id"])
            html_meta = None; checkpoint_sources[old["document_id"]] = checkpoint_source
        parent = make_parent(old, parsed, html_meta)
        parents.append(parent); texts[parent["document_id"]] = parent["_text"]
    parent_map = {row["document_id"]: row for row in parents}

    affected: set[str] = set(html_ids)
    parent_changes: list[dict[str, Any]] = []
    title_changes: list[dict[str, Any]] = []
    for row in parents:
        old = old_parent_map[row["document_id"]]
        if row["parent_document_type"] != old["parent_document_type"]:
            affected.add(row["document_id"])
            parent_changes.append({
                "document_id": row["document_id"], "source_file": row["source_file"], "source_url": row.get("source_url", ""),
                "old_parent_document_type": old["parent_document_type"], "new_parent_document_type": row["parent_document_type"],
                "old_confidence": old.get("classification_confidence"), "new_confidence": row["classification_confidence"],
                "classification_evidence": row["classification_evidence"], "reason": row["classification_reasons"],
                "review_status": "pending",
            })
        if row["parent_title"] != old.get("parent_title", "") or row["parent_title_status"] != old.get("parent_title_status"):
            affected.add(row["document_id"])
            title_changes.append({
                "document_id": row["document_id"], "source_file": row["source_file"],
                "old_title": old.get("parent_title", ""), "new_title": row["parent_title"],
                "old_status": old.get("parent_title_status"), "new_status": row["parent_title_status"],
                "new_source": row["parent_title_source"], "review_status": "pending",
            })

    segments: list[dict[str, Any]] = []
    segment_changes: list[dict[str, Any]] = []
    new_html_relations: list[dict[str, Any]] = []
    for parent in parents:
        if parent["document_id"] in html_ids:
            rows = rebuild_html_segments(parent)
            for row in rows: row["review_status"] = "pending"
            segments.extend(rows)
            continue
        for old in old_segments_by_parent.get(parent["document_id"], []):
            row = transform_segment(old, parent)
            segments.append(row)
            if any(row.get(field) != old.get(field) for field in ("parent_document_type", "segment_type", "accident_category", "content_role", "parent_title", "display_title")):
                affected.add(parent["document_id"])
                segment_changes.append({
                    "document_id": row["document_id"], "parent_document_id": parent["document_id"],
                    "old_segment_type": old.get("segment_type"), "new_segment_type": row["segment_type"],
                    "old_parent_type": old.get("parent_document_type"), "new_parent_type": row["parent_document_type"],
                    "old_content_role": old.get("content_role"), "new_content_role": row["content_role"],
                    "review_status": "pending",
                })

    assign_segment_rag(segments, parent_map)
    # Any eligibility change affects the parent's downstream chunks.
    old_segment_map = {row["document_id"]: row for row in old_segments}
    for row in segments:
        old = old_segment_map.get(row["document_id"])
        if old and bool(old.get("rag_eligible")) != bool(row.get("rag_eligible")):
            affected.add(row["parent_document_id"])
    # Explicit contamination and source-artifact rules always invalidate the
    # old chunks for that parent.
    for chunk in old_chunks:
        if SCRIPT_RE.search(chunk["content"]) or HTML_TAG_RE.search(chunk["content"]) or "pubchem" in chunk["source_file"].lower():
            affected.add(chunk["parent_document_id"])

    chunks, superseded, exact_removed = make_incremental_chunks(old_chunks, segments, parent_map, affected)
    canonical = [row for row in segments if row.get("is_canonical")]
    old_relations = load_jsonl(old_cleaned / "duplicate_segment_relations.jsonl")

    # Strip internal processing fields before persistence and validation.
    persisted_parents = [{k: v for k, v in row.items() if not k.startswith("_")} for row in parents]
    validation = validate_all(persisted_parents, segments, chunks)
    unchanged, raw_integrity = raw_integrity_by_content(manifests, actual_hashes, raw_read_failures)
    raw_checked = raw_integrity["actual_hashed_files"]
    raw_changes = [] if unchanged else [raw_integrity]
    metrics = quality_metrics(persisted_parents, segments, chunks, texts)

    dump_jsonl(cleaned / "parent_documents.jsonl", persisted_parents)
    dump_jsonl(cleaned / "segment_records.jsonl", segments)
    dump_jsonl(cleaned / "canonical_segments.jsonl", canonical)
    dump_jsonl(cleaned / "duplicate_segment_relations.jsonl", [*old_relations, *new_html_relations])
    dump_jsonl(cleaned / "rag_chunks.jsonl", chunks)
    dump_jsonl(cleaned / "superseded_chunks.jsonl", superseded)
    dump_json(cleaned / "affected_parent_ids.json", sorted(affected))
    dump_json(cleaned / "semantic_validation.json", validation)
    dump_json(cleaned / "raw_integrity_check.json", {"unchanged": unchanged, "checked": raw_checked, "changes": raw_changes})
    dump_json(cleaned / "source_path_aliases.json", source_path_aliases)
    dump_json(checkpoints / "run_state.json", {"status": "outputs_generated_pending_tests", "processed_parents": len(parents), "affected_parents": len(affected), "review_status": "pending", "updated_at": now()})

    type_files = {
        "sds_records.jsonl": "sds", "enterprise_plans.jsonl": "enterprise_plan",
        "enterprise_group_plans.jsonl": "enterprise_group_plan", "accident_cases.jsonl": "accident_case",
        "excluded_irrelevant.jsonl": "excluded_irrelevant", "chemical_references.jsonl": "chemical_reference",
        "regulations.jsonl": "regulation", "government_plans.jsonl": "government_or_regional_plan",
        "guidance_references.jsonl": "guidance_reference",
    }
    for filename, ptype in type_files.items():
        dump_jsonl(cleaned / filename, (row for row in persisted_parents if row["parent_document_type"] == ptype))

    # Data-quality queue is deliberately separate from professional content.
    data_quality = []
    for row in persisted_parents:
        reasons = []
        if row["parse_status"] != "success": reasons.append(row["parse_status"])
        if row["parent_title_status"] != "valid": reasons.append(f"parent_title_{row['parent_title_status']}")
        if row["parent_document_type"] == "manual_review": reasons.append("parent_type_manual_review")
        if reasons: data_quality.append({"document_id": row["document_id"], "parent_document_id": row["document_id"], "reasons": reasons, "source_file": row["source_file"], "review_status": "pending"})
    dump_jsonl(cleaned / "data_quality_high_risk.jsonl", data_quality)

    parent_counts = Counter(row["parent_document_type"] for row in persisted_parents)
    segment_counts = Counter(row["segment_type"] for row in segments)
    chunk_parent_counts = Counter(row["parent_document_type"] for row in chunks)
    chunk_segment_counts = Counter(row["segment_type"] for row in chunks)
    recoveries = [row for row in parent_changes if row["new_parent_document_type"] == "sds"]
    sds_folder_non_sds = [row for row in persisted_parents if "/SDS/" in row["source_file"] and row["parent_document_type"] != "sds"]

    rows_report(reports / "parent_type_changes_review.md", "全部父类型变化审核包", parent_changes, ["document_id", "source_file", "source_url", "old_parent_document_type", "new_parent_document_type", "old_confidence", "new_confidence", "classification_evidence", "reason"])
    rows_report(reports / "parent_title_changes_review.md", "全部父标题变化审核包", title_changes, ["document_id", "source_file", "old_title", "new_title", "old_status", "new_status", "new_source"])
    rows_report(reports / "segment_semantic_changes_report.md", "Segment语义类型变化", segment_changes, ["document_id", "parent_document_id", "old_segment_type", "new_segment_type", "old_parent_type", "new_parent_type", "old_content_role", "new_content_role"])
    rows_report(reports / "sds_recovery_report.md", "SDS恢复报告", recoveries, ["document_id", "source_file", "old_parent_document_type", "new_parent_document_type", "classification_evidence"])
    rows_report(reports / "all_non_sds_files_under_sds_folder.md", "SDS目录下全部非SDS文件", sds_folder_non_sds, ["document_id", "source_file", "parent_document_type", "parent_title", "classification_evidence"])
    rows_report(reports / "html_semantic_cleaning_report.md", "HTML正文净化报告", html_audits, ["document_id", "source_file", "parse_status", "html_page_type", "text_length", "javascript_hits", "html_tag_hits", "navigation_hits", "removed_script_lines", "removed_navigation_lines", "warnings"])

    for filename, title, ptype in (
        ("all_sds_parent_review.md", "全部SDS父文档审核包", "sds"),
        ("all_enterprise_plan_review.md", "全部企业预案审核包", "enterprise_plan"),
        ("all_accident_case_review.md", "全部事故案例审核包", "accident_case"),
        ("all_excluded_irrelevant_review.md", "全部无关资料审核包", "excluded_irrelevant"),
        ("all_chemical_reference_review.md", "全部化学品基础参考审核包", "chemical_reference"),
    ):
        review_package(reports / filename, title, [row for row in persisted_parents if row["parent_document_type"] == ptype], "parent")
    parent_sample = stratified_sample(persisted_parents, "parent_document_type", 100, SEED)
    segment_sample = stratified_sample(canonical, "segment_type", 100, SEED)
    chunk_sample = stratified_sample(chunks, "parent_document_type", 200, SEED)
    review_package(reports / "parent_sample_100.md", "父文档分层抽查100条", parent_sample, "parent")
    review_package(reports / "segment_sample_100.md", "Segment分层抽查100条", segment_sample, "segment")
    review_package(reports / "rag_chunk_sample_200.md", "RAG Chunk分层抽查200条", chunk_sample, "chunk")

    write_md(reports / "classification_propagation_audit.md", "分类向下传播审计", [
        f"- 父文档类型变化：{len(parent_changes)}",
        f"- Segment语义变化：{len(segment_changes)}",
        f"- 受影响父文档：{len(affected)}",
        f"- 父/Segment不一致：{metrics['parent_segment_type_inconsistency']}",
        f"- 父/Segment/Chunk不一致：{metrics['parent_segment_chunk_type_inconsistency']}",
        f"- 已废弃旧Chunk：{len(superseded)}",
        f"- 保留或重建Chunk：{len(chunks)}",
    ])
    lengths = sorted(len(row["content"]) for row in chunks)
    percentile = lambda p: lengths[min(len(lengths) - 1, max(0, math.ceil(len(lengths) * p) - 1))] if lengths else 0
    write_md(reports / "rag_semantic_quality_report.md", "RAG语义内容质量报告", [
        f"- 总Chunk：{len(chunks)}\n- 旧Chunk废弃：{len(superseded)}\n- 增量重建中精确重复排除：{exact_removed}",
        f"- 空Chunk：{metrics['empty_chunks']}\n- p50字符：{percentile(.5)}\n- p90字符：{percentile(.9)}\n- p95字符：{percentile(.95)}",
        f"- HTML脚本/导航Chunk：{metrics['html_script_navigation_chunks']}\n- front matter/人员Chunk：{metrics['front_matter_personnel_chunks']}\n- PubChem Chunk：{metrics['pubchem_chunks']}\n- rec/seg重复Chunk：{metrics['rec_seg_duplicate_chunks']}",
        "## 按父类型\n\n" + markdown_count(chunk_parent_counts),
        "## 按Segment类型\n\n" + markdown_count(chunk_segment_counts),
    ])

    # pytest is intentionally last so generated-output regression tests can
    # inspect the semantic-fixed artifacts.
    pytest_result = run_pytest(root)
    acceptance = {
        "1_complete_sds_not_guidance": metrics["strong_sds_as_guidance"] == 0,
        "2_complete_sds_not_regulation": metrics["strong_sds_as_regulation"] == 0,
        "3_complete_sds_not_excluded": metrics["strong_sds_as_excluded"] == 0,
        "4_compilation_guide_not_enterprise": not any(row["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"} and re.search(r"GB(?:/T)?\s*29639|编制导则", row["parent_title"], re.I) for row in persisted_parents),
        "5_enterprise_plan_not_accident_case": not any(row["parent_document_type"] == "accident_case" and re.search(r"应急预案|现场处置方案", Path(row["source_file"]).stem) for row in persisted_parents),
        "6_case_section_only_under_case": not any(row["segment_type"] == "accident_case_section" and row["parent_document_type"] != "accident_case" for row in segments),
        "7_no_body_sentence_parent_title": metrics["parent_title_body_sentence"] == 0,
        "8_no_legal_article_parent_title": metrics["parent_title_legal_article"] == 0,
        "9_no_page_or_sds_section_parent_title": metrics["parent_title_page_or_sds_section"] == 0,
        "10_no_javascript_navigation_chunk": metrics["html_script_navigation_chunks"] == 0,
        "11_no_front_matter_personnel_chunk": metrics["front_matter_personnel_chunks"] == 0,
        "12_no_pubchem_chunk": metrics["pubchem_chunks"] == 0,
        "13_type_consistency": metrics["parent_segment_type_inconsistency"] == 0 and metrics["parent_segment_chunk_type_inconsistency"] == 0,
        "14_no_empty_chunk": metrics["empty_chunks"] == 0,
        "15_no_rec_seg_duplicate_chunk": metrics["rec_seg_duplicate_chunks"] == 0,
        "16_pydantic_all_pass": validation["failure_count"] == 0,
        "17_pytest_all_pass": pytest_result["passed"],
        "18_all_pending": all(row.get("review_status") == "pending" for row in [*persisted_parents, *segments, *chunks]),
        "19_raw_unchanged": unchanged,
        "20_no_embedding": not any(cleaned.glob("*embedding*")),
        "21_no_database_import": True,
    }
    passed = all(acceptance.values())
    dump_json(cleaned / "semantic_metrics.json", metrics)
    dump_json(cleaned / "acceptance_checks.json", {"passed": passed, "checks": acceptance})
    dump_json(cleaned / "pytest_result.json", pytest_result)
    write_md(reports / "acceptance_report.md", "语义修复正式验收", [
        f"结论：**{'通过' if passed else '未通过'}**。",
        "\n".join(f"- [{'x' if ok else ' '}] {name}" for name, ok in acceptance.items()),
        "本报告是自动验收，不代表已完成人工专业审核。",
    ])
    write_md(reports / "regression_test_report.md", "回归测试报告", [f"- pytest returncode：{pytest_result['returncode']}\n- 通过：{pytest_result['passed']}\n\n```text\n{pytest_result['output']}\n```"])
    write_md(reports / "semantic_fix_run_report.md", "全量语义分类与RAG内容质量最终修复报告", [
        f"- 父文档：{len(parents)}\n- Segment：{len(segments)}\n- canonical Segment：{len(canonical)}\n- RAG Chunk：{len(chunks)}",
        f"- 父类型变化：{len(parent_changes)}\n- 父标题变化：{len(title_changes)}\n- 受影响父文档：{len(affected)}\n- HTML本地重新净化：{len(html_audits)}\n- 按SHA解析真实文件路径：{len(source_path_aliases)}",
        "## 父类型分布\n\n" + markdown_count(parent_counts),
        "## Segment分布\n\n" + markdown_count(segment_counts),
        f"- SDS结构识别召回：{metrics['sds_structure_recall']:.2%}\n- 企业预案自动假阳性指标：{metrics['enterprise_false_positive']}\n- 事故案例自动假阳性指标：{metrics['accident_case_false_positive']}",
        f"- Pydantic失败：{validation['failure_count']}\n- pytest通过：{pytest_result['passed']}\n- raw校验未变：{unchanged}\n- 最终自动验收：{passed}",
        "未修改 raw，未生成 Embedding，未导入数据库或知识库；所有记录仍为 pending。",
    ])
    dump_json(checkpoints / "run_state.json", {"status": "complete" if passed else "acceptance_failed", "processed_parents": len(parents), "affected_parents": len(affected), "acceptance_passed": passed, "review_status": "pending", "updated_at": now()})
    return {"passed": passed, "acceptance": acceptance, "metrics": metrics, "pytest": pytest_result, "validation": validation, "counts": {"parents": len(parents), "segments": len(segments), "canonical_segments": len(canonical), "chunks": len(chunks), "affected_parents": len(affected), "parent_changes": len(parent_changes), "title_changes": len(title_changes)}}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=ROOT)
    return value


def main() -> None:
    args = parser().parse_args()
    result = run(args.root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
