"""Targeted repair and re-acceptance of the first full cleaning run.

This runner reuses the first-run manifests and parsed checkpoints.  It only
reopens HTML records which used source-record fallback and PDFs which failed,
and it never writes to the raw tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError
from datasketch import MinHashLSH
from pypdf import PdfReader
from rapidfuzz.fuzz import ratio as fuzzy_ratio

from .full_runner_v1 import markdown_table, split_for_chunks, stable_id
from .deduplication import minhash
from .models_repair_v1 import RepairedChunk, RepairedParent, RepairedSegment
from .normalization import normalize_text
from .repair_rules_v1 import (
    PARENT_TYPES,
    SEGMENT_TYPES,
    accident_category_repaired,
    classify_parent_repaired,
    classify_segment_repaired,
    data_quality_reasons,
    is_system_file,
    parse_html_local_repaired,
    professional_review_reasons,
    recover_parent_title,
    segment_is_rag_eligible,
    text_hash,
)


SEED = 20260824
SYSTEM_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SDS_HEADING_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十0-9]+[部分、.．\s]*)?(化学品及企业标识|化学品与厂商资料|危险性概述|危害辨识资料|成分.?组成信息|急救措施|消防措施|泄漏应急处理|洩漏處理方法|操作处置与储存|接触控制.?个体防护|理化特性|稳定性和反应性|毒理学信息|生态学信息|废弃处置|运输信息|法规信息|其他信息)\s*$",
    re.I,
)
GENERIC_HEADING_RE = re.compile(
    r"^\s*((?:第[一二三四五六七八九十百0-9]+[章节部分]\s*[^。；;：:]{0,40}|\d+(?:[.．]\d+){0,3}\s+[^。；;：:]{2,45}|[一二三四五六七八九十]+[、.．]\s*[^。；;：:]{2,45}|(?:事故经过|事故原因|原因分析|责任追究|整改措施|防范措施|事故教训|编制目的|适用范围|风险分析|事故风险分析|组织机构(?:和|与)?职责|应急响应|应急处置|处置措施|应急保障|附则)))\s*$"
)
ARTICLE_RE = re.compile(r"(?m)^\s*(第[一二三四五六七八九十百零〇0-9]+条[^\n]*)")
PROFESSIONAL_RE = re.compile(r"急救|医疗|消防|灭火|堵漏|泄漏处置|个人防护|防护服|呼吸器|应急响应|疏散|警戒|救援")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def dump_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def magic_bytes(path: Path, length: int = 16) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(length).hex()
    except OSError:
        return "unavailable"


def clean_preview(text: str, length: int = 420) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:length]


def write_md(path: Path, title: str, sections: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join([f"# {title}", *sections]) + "\n", encoding="utf-8")


def checkpoint_payload(parser: str, text: str, warnings: list[str], page_count: int = 0, **extra: Any) -> dict[str, Any]:
    normalized = normalize_text(text)
    payload = {
        "parser": parser,
        "parser_version": "targeted-repair-v1",
        "page_count": page_count,
        "raw_text": text,
        "normalized_text": normalized,
        "text_length": len(normalized),
        "parse_status": "success" if len(normalized) >= 120 else "failed",
        "warnings": warnings,
        "blocks": [],
        "is_scanned_document": parser == "macos_vision_ocr",
        "reprocessed_at": now(),
    }
    payload.update(extra)
    return payload


def build_vision_binary(root: Path, checkpoint_dir: Path) -> Path:
    binary = checkpoint_dir / "bin" / "macos_vision_ocr"
    binary.parent.mkdir(parents=True, exist_ok=True)
    source = root / "pipeline_v2" / "macos_vision_ocr.swift"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        result = subprocess.run(["/usr/bin/swiftc", "-O", str(source), "-o", str(binary)], capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(f"Vision OCR compile failed: {result.stderr[:500]}")
    return binary


def pdf_page_count(path: Path) -> int:
    try:
        return len(PdfReader(str(path), strict=False).pages)
    except Exception:
        return 0


def extract_pdf_text(path: Path) -> tuple[str, int, str]:
    try:
        reader = PdfReader(str(path), strict=False)
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages)
        if len(normalize_text(text)) >= 120:
            return text, len(reader.pages), "pypdf_repair"
    except Exception:
        pass
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n\n".join(pages)
        if len(normalize_text(text)) >= 120:
            return text, len(pages), "pdfplumber_repair"
    except Exception:
        pass
    return "", pdf_page_count(path), ""


def _parse_vision_output(output: str) -> list[str]:
    pages: list[str] = []
    current: list[str] = []
    for line in output.splitlines():
        if line.startswith("===PAGE:"):
            if current:
                pages.append("\n".join(current))
            current = []
        elif not line.startswith("===ERROR:"):
            current.append(line)
    if current:
        pages.append("\n".join(current))
    return pages


def ocr_pdf(path: Path, page_count: int, vision_binary: Path, checkpoint_dir: Path, document_id: str) -> tuple[str, int, list[str]]:
    if page_count <= 0:
        return "", 0, ["pdf_page_count_unavailable"]
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        raise RuntimeError("pdftoppm is required for PDF OCR; install Poppler first")
    temp_root = checkpoint_dir / "tmp" / "pdfs" / document_id
    temp_root.mkdir(parents=True, exist_ok=True)
    all_pages: list[str] = []
    warnings: list[str] = []
    batch_size = 20
    for first in range(1, page_count + 1, batch_size):
        last = min(page_count, first + batch_size - 1)
        prefix = temp_root / f"page-{first:04d}"
        render = subprocess.run(
            [pdftoppm, "-f", str(first), "-l", str(last), "-jpeg", "-r", "125", str(path), str(prefix)],
            capture_output=True, text=True, timeout=max(180, (last - first + 1) * 20),
        )
        images = sorted(temp_root.glob(f"{prefix.name}-*.jpg"))
        if render.returncode != 0 or not images:
            warnings.append(f"render_failed_pages_{first}_{last}:{render.stderr[:160]}")
            continue
        result = subprocess.run([str(vision_binary), *map(str, images)], capture_output=True, text=True, timeout=max(300, len(images) * 30))
        batch_pages = _parse_vision_output(result.stdout)
        all_pages.extend(batch_pages)
        if result.returncode != 0:
            warnings.append(f"vision_ocr_nonzero_pages_{first}_{last}:{result.stderr[:160]}")
        for image in images:
            try:
                image.unlink()
            except OSError:
                pass
        print(f"OCR {document_id}: pages {first}-{last}/{page_count}", flush=True)
    return "\n\n".join(all_pages), len(all_pages), warnings


def reprocess_html(record: dict[str, Any], checkpoint_dir: Path) -> dict[str, Any]:
    path = Path(record["source_file"])
    try:
        parsed = parse_html_local_repaired(path.read_bytes())
        payload = checkpoint_payload("html_local_repaired", parsed["raw_text"], ["targeted_html_fallback_reprocess"], **{k: v for k, v in parsed.items() if k not in {"raw_text", "normalized_text", "text_length", "parser", "parse_status"}})
    except Exception as exc:
        payload = checkpoint_payload("html_local_repaired", "", [f"targeted_html_failed:{type(exc).__name__}:{str(exc)[:180]}"])
    dump_json(checkpoint_dir / "parsed_documents" / f"{record['document_id']}.json", payload)
    return payload


def reprocess_pdf(record: dict[str, Any], checkpoint_dir: Path, vision_binary: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(record["source_file"])
    report = {"document_id": record["document_id"], "source_file": str(path), "magic_bytes": "", "detected_mime_type": "", "original_parser": record.get("parser_selected", ""), "fallback_parser": "", "ocr_used": False, "result": "", "recoverable": False, "final_status": "pending"}
    if not record.get("sha256"):
        payload = checkpoint_payload("unavailable_local_placeholder", "", ["unavailable_local_placeholder"])
        report.update(detected_mime_type="unavailable", result="cloud_or_dataless_placeholder", final_status="unavailable_local_placeholder")
        dump_json(checkpoint_dir / "parsed_documents" / f"{record['document_id']}.json", payload)
        return payload, report
    try:
        head = path.read_bytes()[:64]
    except Exception as exc:
        payload = checkpoint_payload("corrupted_pdf_pending", "", [f"pdf_read_failed:{type(exc).__name__}"])
        report.update(result="read_failed", final_status="corrupted_pdf_pending")
        dump_json(checkpoint_dir / "parsed_documents" / f"{record['document_id']}.json", payload)
        return payload, report
    report["magic_bytes"] = head[:16].hex()
    if not head.startswith(b"%PDF"):
        if b"html" in head.lower() or head.lstrip().startswith((b"<", b"<!")):
            parsed = parse_html_local_repaired(path.read_bytes())
            payload = checkpoint_payload("html_local_repaired_from_pdf", parsed["raw_text"], ["html_disguised_as_pdf", "targeted_failed_pdf_reprocess"], html_page_type=parsed["html_page_type"], encoding=parsed["encoding"])
            report.update(detected_mime_type="text/html", fallback_parser="html_local_repaired", result="recovered_as_html" if payload["parse_status"] == "success" else parsed["html_page_type"], recoverable=payload["parse_status"] == "success", final_status=payload["parse_status"])
        else:
            payload = checkpoint_payload("corrupted_pdf_pending", "", ["pdf_magic_bytes_mismatch"])
            report.update(detected_mime_type="application/octet-stream", result="not_a_pdf", final_status="corrupted_pdf_pending")
        dump_json(checkpoint_dir / "parsed_documents" / f"{record['document_id']}.json", payload)
        return payload, report
    report["detected_mime_type"] = "application/pdf"
    text, pages, parser = extract_pdf_text(path)
    if len(normalize_text(text)) >= 120:
        payload = checkpoint_payload(parser, text, ["targeted_failed_pdf_text_recovery"], page_count=pages)
        report.update(fallback_parser=parser, result="text_layer_recovered", recoverable=True, final_status="success")
    else:
        text, recovered_pages, warnings = ocr_pdf(path, pages, vision_binary, checkpoint_dir, record["document_id"])
        payload = checkpoint_payload("macos_vision_ocr", text, ["targeted_failed_pdf_ocr", *warnings], page_count=pages, ocr_pages_recovered=recovered_pages)
        report.update(fallback_parser="macos_vision_ocr", ocr_used=True, result="ocr_recovered" if payload["parse_status"] == "success" else "ocr_failed", recoverable=payload["parse_status"] == "success", final_status=payload["parse_status"] if pages else "corrupted_pdf_pending")
    dump_json(checkpoint_dir / "parsed_documents" / f"{record['document_id']}.json", payload)
    return payload, report


def split_structural_sections(text: str, parent_type: str) -> list[tuple[str, str, int, int, str]]:
    """Return conservative real sections; weak fragments stay in the full segment."""
    matches: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?m)^([^\n]{1,100})$", text):
        line = match.group(1).strip()
        if parent_type == "sds":
            valid = bool(SDS_HEADING_RE.match(line))
        elif parent_type == "regulation":
            valid = bool(ARTICLE_RE.match(line))
        else:
            valid = bool(GENERIC_HEADING_RE.match(line)) and not re.search(r"负责|立即|按照|万元|小时以上", line)
        if valid:
            matches.append((match.start(), match.end(), line))
    sections: list[tuple[str, str, int, int, str]] = []
    for index, (start, heading_end, heading) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if len(content) < 80:
            continue
        if parent_type == "sds": stype = "sds_section"
        elif parent_type == "regulation": stype = "regulation_article"
        elif parent_type == "accident_case": stype = "accident_case_section"
        elif parent_type == "guidance_reference": stype = "guidance_section"
        elif re.search(r"现场处置方案|现场处置要点", heading): stype = "onsite_disposal_plan"
        elif re.search(r"专项应急预案|事故应急预案", heading): stype = "embedded_special_section"
        else: stype = "guidance_section"
        sections.append((heading, content, start, end, stype))
    return sections


def build_parent(
    manifest: dict[str, Any], old_parent: dict[str, Any] | None, parsed: dict[str, Any], raw_dir: Path,
) -> dict[str, Any] | None:
    if is_system_file(manifest["source_file"]):
        return None
    text = normalize_text(str(parsed.get("normalized_text") or parsed.get("raw_text") or ""))
    existing_title = str((old_parent or {}).get("canonical_title") or (old_parent or {}).get("title") or (manifest.get("source_titles") or [""])[0])
    classification = classify_parent_repaired(
        text=text,
        title=existing_title,
        source_file=manifest["source_file"],
        parse_status=str(parsed.get("parse_status") or manifest.get("parse_status") or "failed"),
    )
    ptype = classification["parent_document_type"] or "reject"
    parent_title, title_status, title_source = recover_parent_title(text, existing_title, manifest["source_file"], ptype)
    category, secondary = accident_category_repaired(parent_title, text, ptype)
    old_type = str((old_parent or {}).get("parent_document_type") or "")
    warnings = list(dict.fromkeys([*(parsed.get("warnings") or []), *((old_parent or {}).get("warnings") or [])]))
    parse_status = str(parsed.get("parse_status") or "failed")
    requires_review = bool(classification.get("requires_manual_review") or title_status != "valid" or parse_status != "success" or ptype in {"manual_review", "reject"})
    source_file = str(Path(manifest["source_file"]).resolve())
    row = {
        "record_level": "parent_document",
        "document_id": manifest["document_id"],
        "parent_document_id": manifest["document_id"],
        "source_file": source_file,
        "relative_path": str(Path(source_file).relative_to(raw_dir)),
        "sha256": manifest.get("sha256"),
        "file_extension": manifest.get("file_extension", ""),
        "source_url": manifest.get("source_url", ""),
        "source_urls": manifest.get("source_urls", []),
        "source_url_status": manifest.get("source_url_status", "missing"),
        "parent_document_type": ptype,
        "parent_type_locked": True,
        "parent_type_evidence_scope": "dominant_file_structure_repaired",
        "classification_evidence": classification["evidence"],
        "classification_confidence": classification["confidence"],
        "classification_reasons": [f"repaired_from_{old_type or 'unknown'}_to_{ptype}"],
        "old_parent_document_type": old_type,
        "old_classification_confidence": (old_parent or {}).get("classification_confidence"),
        "classification_conflict": ptype == "manual_review",
        "embedded_content_types": [],
        "parent_title": parent_title,
        "parent_title_status": title_status,
        "parent_title_source": title_source,
        "canonical_title": parent_title,
        "title": parent_title,
        "parse_status": parse_status,
        "parser_selected": parsed.get("parser", manifest.get("parser_selected", "")),
        "parser_version": parsed.get("parser_version", ""),
        "page_count": int(parsed.get("page_count") or 0),
        "text_length": len(text),
        "accident_category": category,
        "secondary_topic": secondary,
        "content_role": (
            "regulatory_reference" if ptype == "regulation" else
            "safety_data" if ptype == "sds" else
            "accident_case_reference" if ptype == "accident_case" else
            "excluded_irrelevant" if ptype == "excluded_irrelevant" else
            "guidance_reference" if ptype == "guidance_reference" else
            "emergency_plan"
        ),
        "warnings": warnings,
        "requires_manual_review": requires_review,
        "quality_grade": "rejected" if ptype == "reject" else "manual_review" if requires_review else "gold_candidate" if classification["confidence"] >= 0.88 else "silver_candidate",
        "review_status": "pending",
        # Internal processing text is removed before writing parent_documents.
        "_text": text,
        "_blocks": parsed.get("blocks") or [],
    }
    RepairedParent.model_validate({k: v for k, v in row.items() if not k.startswith("_")})
    return row


def make_segment_row(
    parent: dict[str, Any], *, segment_id: str, text: str, section_title: str,
    segment_type: str, start: int, end: int, origin: str, is_parent_full: bool = False,
) -> dict[str, Any]:
    normalized = normalize_text(text)
    category, secondary = accident_category_repaired(section_title or parent["parent_title"], normalized, parent["parent_document_type"])
    display = f"{parent['parent_title']}｜{section_title}" if section_title else parent["parent_title"]
    classification_type, audit = classify_segment_repaired(
        title=section_title or parent["parent_title"], text=normalized,
        parent_type=parent["parent_document_type"], is_parent_full=is_parent_full,
    )
    # Generated structural sections have already passed stronger boundary
    # checks; otherwise the local-text classifier is authoritative.
    final_type = segment_type if origin == "generated_structural_section" and segment_type in SEGMENT_TYPES else classification_type
    if is_parent_full:
        final_type = "full_document"
    content_hash = text_hash(normalized)
    row = {
        "record_level": "segment_record",
        "document_id": segment_id,
        "parent_document_id": parent["parent_document_id"],
        "canonical_segment_id": segment_id,
        "is_canonical": True,
        "duplicate_segment_ids": [],
        "duplicate_reason": "",
        "normalized_content_hash": content_hash,
        "parent_document_type": parent["parent_document_type"],
        "segment_type": final_type,
        "parent_title": parent["parent_title"],
        "parent_title_status": parent["parent_title_status"],
        "section_title": section_title,
        "display_title": display,
        "title_status": "valid" if section_title else "not_applicable",
        "content": normalized,
        "raw_text": text,
        "accident_category": category,
        "secondary_topic": secondary,
        "content_role": (
            "safety_data_section" if final_type in {"sds_section", "appendix_sds", "embedded_sds"} else
            "regulatory_article" if final_type == "regulation_article" else
            "accident_case_reference" if final_type == "accident_case_section" else
            "professional_guidance" if final_type == "guidance_section" else
            "context_only" if final_type in {"reference_sentence", "list_fragment", "toc_fragment"} else
            parent["content_role"]
        ),
        "source_file": parent["source_file"],
        "relative_path": parent["relative_path"],
        "source_url": parent["source_url"],
        "source_page_start": None,
        "source_page_end": None,
        "char_start": max(0, start),
        "char_end": max(start, end),
        "segment_start": max(0, start),
        "segment_end": max(start, end),
        "segment_classification_text_source": "raw_text",
        "segment_local_text_length": len(normalized),
        "parent_context_used": False,
        "segment_classification_audit": audit,
        "source_origin": origin,
        "is_parent_full_document": is_parent_full,
        "warnings": [],
        "quality_grade": parent["quality_grade"],
        "review_status": "pending",
        "requires_manual_review": parent["requires_manual_review"],
        "rag_eligible": False,
    }
    return row


def source_child_segments(parent: dict[str, Any], old_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for old in old_segments:
        if old.get("parent_document_id") != parent["parent_document_id"] or old.get("is_parent_full_document"):
            continue
        text = str(old.get("raw_text") or old.get("normalized_text") or "")
        if len(normalize_text(text)) < 20:
            continue
        title = str(old.get("title") or old.get("original_title") or "").strip()
        segment_id = stable_id("rec", parent["parent_document_id"], str(old.get("document_id")), text_hash(text))
        stype, _ = classify_segment_repaired(title=title, text=text, parent_type=parent["parent_document_type"], is_parent_full=False)
        row = make_segment_row(
            parent, segment_id=segment_id, text=text, section_title=title,
            segment_type=stype, start=int(old.get("segment_start") or 0),
            end=int(old.get("segment_end") or len(text)), origin="source_record_repaired",
        )
        rows.append(row)
    return rows


def build_segments(parent: dict[str, Any], old_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = parent["_text"]
    if parent["parse_status"] != "success" or not text:
        return []
    full_id = stable_id("seg", parent["parent_document_id"], "parent_full_repaired")
    rows = [make_segment_row(
        parent, segment_id=full_id, text=text, section_title="", segment_type="full_document",
        start=0, end=len(text), origin="parent_parsed_text", is_parent_full=True,
    )]
    for order, (heading, content, start, end, stype) in enumerate(split_structural_sections(text, parent["parent_document_type"])):
        rows.append(make_segment_row(
            parent,
            segment_id=stable_id("seg", parent["parent_document_id"], str(order), heading, text_hash(content)),
            text=content, section_title=heading, segment_type=stype,
            start=start, end=end, origin="generated_structural_section",
        ))
    rows.extend(source_child_segments(parent, old_segments))
    return rows


def canonicalize_segments(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    relations: list[dict[str, Any]] = []
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in segments:
        by_parent[row["parent_document_id"]].append(row)
    priority = {"generated_structural_section": 0, "parent_parsed_text": 1, "source_record_repaired": 2}
    for parent_id, members in by_parent.items():
        exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in members:
            exact[row["normalized_content_hash"]].append(row)
        for digest, group in exact.items():
            if len(group) < 2:
                continue
            ordered = sorted(group, key=lambda row: (priority.get(row["source_origin"], 9), -len(row["content"]), row["document_id"]))
            canonical = ordered[0]
            for duplicate in ordered[1:]:
                duplicate.update(is_canonical=False, canonical_segment_id=canonical["document_id"], duplicate_reason="exact_normalized_content")
                canonical["duplicate_segment_ids"].append(duplicate["document_id"])
                relations.append({"parent_document_id": parent_id, "canonical_segment_id": canonical["document_id"], "duplicate_segment_id": duplicate["document_id"], "duplicate_reason": "exact_normalized_content", "similarity": 1.0, "review_status": "pending"})
        # Near dedup is deliberately limited to rec/seg cross-layer pairs with
        # comparable lengths.  It cannot merge unrelated documents.
        canonical_members = [row for row in members if row["is_canonical"]]
        recs = [row for row in canonical_members if row["document_id"].startswith("rec-")]
        segs = [row for row in canonical_members if row["document_id"].startswith("seg-")]
        for rec in recs:
            best: tuple[float, dict[str, Any]] | None = None
            for seg in segs:
                length_ratio = min(len(rec["content"]), len(seg["content"])) / max(1, max(len(rec["content"]), len(seg["content"])))
                if length_ratio < 0.88:
                    continue
                similarity = fuzzy_ratio(rec["content"], seg["content"]) / 100
                if similarity >= 0.95 and (best is None or similarity > best[0]):
                    best = (similarity, seg)
            if best:
                similarity, seg = best
                rec.update(is_canonical=False, canonical_segment_id=seg["document_id"], duplicate_reason="near_rec_seg_layer_duplicate")
                seg["duplicate_segment_ids"].append(rec["document_id"])
                relations.append({"parent_document_id": parent_id, "canonical_segment_id": seg["document_id"], "duplicate_segment_id": rec["document_id"], "duplicate_reason": "near_rec_seg_layer_duplicate", "similarity": round(similarity, 4), "review_status": "pending"})
    return segments, relations


def assign_rag_eligibility(segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]]) -> None:
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in segments:
        by_parent[row["parent_document_id"]].append(row)
    for parent_id, members in by_parent.items():
        parent = parents[parent_id]
        structured = [row for row in members if row["is_canonical"] and row["source_origin"] == "generated_structural_section" and row["segment_type"] not in {"full_document", "toc_fragment", "list_fragment", "reference_sentence"} and len(row["content"]) >= 80]
        for row in members:
            eligible = segment_is_rag_eligible(row, parent)
            if row["segment_type"] == "full_document" and structured:
                eligible = False
                row["warnings"].append("full_document_superseded_by_structured_segments")
            if row["source_origin"] == "source_record_repaired":
                # Source-record fragments remain traceable but never compete
                # with the physical file segment or generated file sections.
                eligible = False
            row["rag_eligible"] = eligible
            if not row["is_canonical"]:
                row["warnings"].append("duplicate_segment_excluded_from_rag")


def make_chunks(segments: list[dict[str, Any]], parents: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    chunks: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    exact_removed = 0
    for segment in segments:
        if not segment.get("rag_eligible"):
            continue
        parent = parents[segment["parent_document_id"]]
        for index, (local_start, local_end, content) in enumerate(split_for_chunks(segment["content"], max_chars=1800, overlap=100)):
            content = content.strip()
            if len(content) < 50:
                continue
            digest = text_hash(content)
            if digest in seen_hashes:
                exact_removed += 1
                continue
            seen_hashes[digest] = segment["document_id"]
            display = segment["display_title"] or parent["parent_title"]
            row = {
                "record_level": "rag_chunk",
                "chunk_id": stable_id("chunk", segment["document_id"], str(index), digest),
                "canonical_segment_id": segment["canonical_segment_id"],
                "parent_document_id": segment["parent_document_id"],
                "parent_title": parent["parent_title"],
                "section_title": segment["section_title"],
                "display_title": display,
                "content": content,
                "parent_document_type": parent["parent_document_type"],
                "segment_type": segment["segment_type"],
                "accident_category": segment["accident_category"],
                "secondary_topic": segment["secondary_topic"],
                "content_role": segment["content_role"],
                "chemical_name": "",
                "source_file": parent["source_file"],
                "relative_path": parent["relative_path"],
                "source_url": parent["source_url"],
                "source_page_start": segment.get("source_page_start"),
                "source_page_end": segment.get("source_page_end"),
                "char_start": segment["char_start"] + local_start,
                "char_end": segment["char_start"] + local_end,
                "normalized_content_hash": digest,
                "quality_grade": segment["quality_grade"],
                "review_status": "pending",
                "warnings": list(segment["warnings"]),
            }
            chunks.append(row)
    return chunks, exact_removed


def near_duplicate_chunk_relations(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recall with MinHashLSH and confirm with RapidFuzz.

    Exact hashes were already removed.  This pass is audit-only and never
    deletes another chunk automatically.
    """
    lsh = MinHashLSH(threshold=0.90, num_perm=128)
    signatures: dict[str, Any] = {}
    texts: dict[str, str] = {}
    for row in chunks:
        ident = row["chunk_id"]
        text = row["content"]
        signature = minhash(text)
        signatures[ident] = signature
        texts[ident] = text
        lsh.insert(ident, signature)
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for ident, signature in signatures.items():
        for other in lsh.query(signature):
            if ident == other:
                continue
            pair = tuple(sorted((ident, other)))
            if pair in seen:
                continue
            seen.add(pair)
            left, right = texts[pair[0]], texts[pair[1]]
            length_ratio = min(len(left), len(right)) / max(1, max(len(left), len(right)))
            if length_ratio < 0.80:
                continue
            fuzzy = fuzzy_ratio(left, right) / 100
            jaccard = signatures[pair[0]].jaccard(signatures[pair[1]])
            if fuzzy >= 0.92 and jaccard >= 0.88:
                output.append({"left_chunk_id": pair[0], "right_chunk_id": pair[1], "rapidfuzz_ratio": round(fuzzy, 4), "minhash_jaccard": round(jaccard, 4), "status": "manual_review", "review_status": "pending"})
    return output


def stratified_sample(rows: list[dict[str, Any]], type_field: str, target: int, seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(type_field) or "unknown")].append(row)
    selected: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = list(groups[key])
        rng.shuffle(group)
        selected.extend(group[: min(5, len(group))])
    selected_ids = {id(row) for row in selected}
    remaining = [row for row in rows if id(row) not in selected_ids]
    rng.shuffle(remaining)
    selected.extend(remaining[: max(0, target - len(selected))])
    return selected[:target]


def review_package(path: Path, title: str, rows: list[dict[str, Any]], kind: str) -> None:
    sections = [f"生成时间：{now()}\n\n记录数：{len(rows)}\n\n所有记录审核状态：`pending`。"]
    for index, row in enumerate(rows, 1):
        if kind == "parent":
            sections.append(
                f"## {index}. {row.get('parent_title') or '（父标题缺失）'}\n\n"
                f"- document_id：`{row.get('document_id')}`\n"
                f"- 父类型：`{row.get('parent_document_type')}`\n"
                f"- 原父类型：`{row.get('old_parent_document_type')}`\n"
                f"- 来源文件：`{row.get('source_file')}`\n"
                f"- 来源URL：{row.get('source_url') or '缺失'}\n"
                f"- 证据：{'; '.join(row.get('classification_evidence') or [])}\n"
                f"- 置信度：{row.get('classification_confidence')}\n"
                f"- 警告：{'; '.join(row.get('warnings') or []) or '无'}\n"
                f"- 人工结论：________"
            )
        elif kind == "segment":
            sections.append(
                f"## {index}. {row.get('display_title') or '（显示标题缺失）'}\n\n"
                f"- segment_id：`{row.get('document_id')}`\n"
                f"- 父类型 / segment类型：`{row.get('parent_document_type')}` / `{row.get('segment_type')}`\n"
                f"- canonical：`{row.get('is_canonical')}`；RAG候选：`{row.get('rag_eligible')}`\n"
                f"- 来源：`{row.get('source_file')}`\n"
                f"- 字符位置：{row.get('char_start')}—{row.get('char_end')}\n"
                f"- 正文：{clean_preview(row.get('content', ''), 800)}\n"
                f"- 人工结论：________"
            )
        else:
            sections.append(
                f"## {index}. {row.get('display_title') or '（显示标题缺失）'}\n\n"
                f"- chunk_id：`{row.get('chunk_id')}`\n"
                f"- 父类型 / segment类型：`{row.get('parent_document_type')}` / `{row.get('segment_type')}`\n"
                f"- 父标题：{row.get('parent_title')}\n"
                f"- 章节标题：{row.get('section_title') or '无独立章节标题'}\n"
                f"- 来源：`{row.get('source_file')}`\n"
                f"- 页码 / 字符位置：{row.get('source_page_start') or '-'}—{row.get('source_page_end') or '-'} / {row.get('char_start')}—{row.get('char_end')}\n"
                f"- 内容：{clean_preview(row.get('content', ''), 900)}\n"
                f"- 人工结论：________"
            )
    write_md(path, title, sections)


def validate_all(parents: list[dict[str, Any]], segments: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    checked = Counter()
    for name, model, rows in (
        ("RepairedParent", RepairedParent, parents),
        ("RepairedSegment", RepairedSegment, segments),
        ("RepairedChunk", RepairedChunk, chunks),
    ):
        for row in rows:
            checked[name] += 1
            payload = {k: v for k, v in row.items() if not k.startswith("_")}
            try:
                model.model_validate(payload)
            except ValidationError as exc:
                failures.append({"model": name, "record_id": row.get("document_id") or row.get("chunk_id"), "errors": exc.errors(include_url=False)})
    return {"checked": dict(checked), "failure_count": len(failures), "failures": failures[:500]}


def run_pytest(root: Path) -> dict[str, Any]:
    python = root / ".venv-cleaning-v2" / "bin" / "python"
    result = subprocess.run([str(python), "-m", "pytest", "-q"], cwd=root, capture_output=True, text=True, timeout=900)
    combined = (result.stdout + "\n" + result.stderr).strip()
    return {"returncode": result.returncode, "output": combined, "passed": result.returncode == 0}


def raw_unchanged(manifests: list[dict[str, Any]]) -> tuple[bool, list[dict[str, str]], int]:
    changes: list[dict[str, str]] = []
    checked = 0
    for row in manifests:
        # Finder metadata is explicitly ignored by this repair version. It is
        # audited separately and never treated as a source document.
        if is_system_file(row["source_file"]):
            continue
        expected = row.get("sha256")
        if not expected:
            continue
        checked += 1
        try:
            actual = sha256_file(Path(row["source_file"]))
        except Exception as exc:
            changes.append({"source_file": row["source_file"], "reason": f"hash_read_failed:{type(exc).__name__}"})
            continue
        if actual != expected:
            changes.append({"source_file": row["source_file"], "expected": expected, "actual": actual})
    return not changes, changes, checked


def rag_metrics(chunks: list[dict[str, Any]], parents: list[dict[str, Any]], segments: list[dict[str, Any]], exact_removed: int, near_relations: list[dict[str, Any]]) -> dict[str, Any]:
    lengths = sorted(len(row["content"]) for row in chunks)
    def percentile(p: float) -> int:
        if not lengths: return 0
        return lengths[min(len(lengths) - 1, math.ceil(p * len(lengths)) - 1)]
    per_parent = Counter(row["parent_document_id"] for row in chunks)
    counts = sorted(per_parent.values())
    duplicate_hashes = sum(value - 1 for value in Counter(row["normalized_content_hash"] for row in chunks).values() if value > 1)
    rec_seg_duplicates = sum(1 for row in chunks if row["canonical_segment_id"].startswith("rec-") and "duplicate_segment_excluded_from_rag" in row.get("warnings", []))
    return {
        "total_chunks": len(chunks),
        "empty_chunks": sum(not row["content"].strip() for row in chunks),
        "under_50_chars": sum(len(row["content"]) < 50 for row in chunks),
        "over_1800_chars": sum(len(row["content"]) > 1800 for row in chunks),
        "p50_chars": percentile(0.50), "p90_chars": percentile(0.90), "p95_chars": percentile(0.95),
        "average_chunks_per_parent": round(len(chunks) / max(1, len(per_parent)), 2),
        "median_chunks_per_parent": statistics.median(counts) if counts else 0,
        "max_chunks_per_parent": max(counts, default=0),
        "exact_duplicate_chunks": duplicate_hashes,
        "exact_duplicate_chunks_removed": exact_removed,
        "near_duplicate_chunks": len(near_relations),
        "rec_seg_duplicate_chunks": rec_seg_duplicates,
        "without_parent_title": sum(not row.get("parent_title") for row in chunks),
        "without_display_title": sum(not row.get("display_title") for row in chunks),
        "without_source_file": sum(not row.get("source_file") for row in chunks),
        "without_source_url": sum(not row.get("source_url") for row in chunks),
        "without_page_and_char_position": sum(not row.get("source_page_start") and row.get("char_start") is None for row in chunks),
        "by_parent_type": dict(Counter(row["parent_document_type"] for row in chunks)),
        "by_segment_type": dict(Counter(row["segment_type"] for row in chunks)),
        "excluded_toc_fragments": sum(row["segment_type"] == "toc_fragment" and not row.get("rag_eligible") for row in segments),
        "excluded_irrelevant_parents": sum(row["parent_document_type"] == "excluded_irrelevant" for row in parents),
        "excluded_failed_parents": sum(row["parse_status"] != "success" for row in parents),
    }


def classification_review_reports(report_dir: Path, parents: list[dict[str, Any]]) -> None:
    mapping = {
        "enterprise_plan_candidates_review.md": {"enterprise_plan"},
        "enterprise_group_plan_review.md": {"enterprise_group_plan"},
        "sds_parent_review.md": {"sds"},
        "accident_case_review.md": {"accident_case"},
        "government_plan_review.md": {"government_or_regional_plan"},
        "regulation_review.md": {"regulation"},
        "guidance_reference_review.md": {"guidance_reference"},
        "excluded_irrelevant_review.md": {"excluded_irrelevant"},
    }
    for filename, types in mapping.items():
        subset = [row for row in parents if row["parent_document_type"] in types]
        review_package(report_dir / filename, filename.removesuffix(".md"), subset, "parent")


def report_counts_table(counter: Counter | dict[str, int]) -> str:
    return markdown_table(["类型", "数量"], sorted(dict(counter).items()))


def render_queue_report(path: Path, title: str, rows: list[dict[str, Any]]) -> None:
    table_rows = []
    for row in rows:
        table_rows.append((row.get("document_id") or row.get("parent_document_id"), row.get("parent_title") or row.get("display_title") or "", "; ".join(row.get("review_reasons") or []), row.get("source_file", "")))
    write_md(path, title, [f"记录数：{len(rows)}。本报告为自动审核队列，不代表人工专业结论。", markdown_table(["记录ID", "标题", "原因", "来源文件"], table_rows)])


def run_repair(root: Path, raw_dir: Path) -> int:
    cleaned = root / "cleaned_full_v1_repaired"
    reports = root / "reports_full_v1_repaired"
    logs = root / "logs_full_v1_repaired"
    checkpoints = root / "checkpoints_full_v1_repaired"
    for directory in (cleaned, reports, logs, checkpoints / "parsed_documents"):
        directory.mkdir(parents=True, exist_ok=True)

    old_manifest = load_jsonl(root / "manifests_full_v1" / "raw_file_manifest.jsonl")
    old_parents = {row["document_id"]: row for row in load_jsonl(root / "cleaned_full_v1" / "parent_documents.jsonl")}
    old_segments = load_jsonl(root / "cleaned_full_v1" / "segment_records.jsonl")
    if not old_manifest or not old_parents:
        raise RuntimeError("first full-run manifest or parent results are missing")
    print(f"Loaded first run: manifest={len(old_manifest)}, parents={len(old_parents)}, segments={len(old_segments)}", flush=True)

    system_rows = [row for row in old_manifest if is_system_file(row["source_file"])]
    non_system = [row for row in old_manifest if not is_system_file(row["source_file"])]
    dump_jsonl(checkpoints / "ignored_system_files.jsonl", [{**row, "status": "ignored_system_file", "review_status": "pending"} for row in system_rows])

    html_targets = [row for row in non_system if row.get("file_extension") in {"html", "htm"} and row.get("parser_selected") == "source_record_fallback"]
    pdf_targets = [row for row in non_system if row.get("file_extension") == "pdf" and row.get("parse_status") == "failed"]
    vision_binary = build_vision_binary(root, checkpoints) if pdf_targets else Path("")
    targeted: dict[str, dict[str, Any]] = {}
    html_report_rows: list[dict[str, Any]] = []
    for index, row in enumerate(html_targets, 1):
        repaired_checkpoint = checkpoints / "parsed_documents" / f"{row['document_id']}.json"
        payload = json.loads(repaired_checkpoint.read_text(encoding="utf-8")) if repaired_checkpoint.exists() else reprocess_html(row, checkpoints)
        targeted[row["document_id"]] = payload
        html_report_rows.append({"document_id": row["document_id"], "source_file": row["source_file"], "old_parser": row.get("parser_selected"), "new_parser": payload["parser"], "page_type": payload.get("html_page_type", ""), "parse_status": payload["parse_status"], "text_length": payload["text_length"]})
        print(f"HTML fallback {index}/{len(html_targets)}: {row['document_id']} -> {payload['parse_status']}", flush=True)
    pdf_report_rows: list[dict[str, Any]] = []
    for index, row in enumerate(pdf_targets, 1):
        print(f"PDF repair {index}/{len(pdf_targets)}: {row['document_id']}", flush=True)
        repaired_checkpoint = checkpoints / "parsed_documents" / f"{row['document_id']}.json"
        if repaired_checkpoint.exists():
            payload = json.loads(repaired_checkpoint.read_text(encoding="utf-8"))
            parser_name = payload.get("parser", "")
            page_type = payload.get("html_page_type", "")
            report = {"document_id": row["document_id"], "source_file": row["source_file"], "magic_bytes": magic_bytes(Path(row["source_file"])) if row.get("sha256") else "unavailable", "detected_mime_type": "text/html" if "html" in parser_name else "unavailable" if parser_name == "unavailable_local_placeholder" else "application/pdf", "original_parser": row.get("parser_selected", ""), "fallback_parser": parser_name, "ocr_used": parser_name == "macos_vision_ocr", "result": page_type or ("recovered" if payload.get("parse_status") == "success" else parser_name), "recoverable": payload.get("parse_status") == "success", "final_status": payload.get("parse_status", "failed")}
        else:
            payload, report = reprocess_pdf(row, checkpoints, vision_binary)
        targeted[row["document_id"]] = payload
        pdf_report_rows.append(report)

    parents: list[dict[str, Any]] = []
    for index, manifest in enumerate(non_system, 1):
        ident = manifest["document_id"]
        if ident in targeted:
            parsed = targeted[ident]
        else:
            checkpoint = root / "checkpoints_full_v1" / "parsed_documents" / f"{ident}.json"
            parsed = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {"parse_status": "failed", "raw_text": "", "normalized_text": "", "warnings": ["existing_parse_checkpoint_missing"]}
        parent = build_parent(manifest, old_parents.get(ident), parsed, raw_dir)
        if parent:
            parents.append(parent)
        if index % 100 == 0:
            print(f"Parent reclassification: {index}/{len(non_system)}", flush=True)
    parent_map = {row["parent_document_id"]: row for row in parents}

    segments: list[dict[str, Any]] = []
    for index, parent in enumerate(parents, 1):
        segments.extend(build_segments(parent, old_segments))
        if index % 100 == 0:
            print(f"Segment rebuild: {index}/{len(parents)}", flush=True)
    segments, duplicate_relations = canonicalize_segments(segments)
    assign_rag_eligibility(segments, parent_map)
    chunks, exact_chunks_removed = make_chunks(segments, parent_map)
    near_chunk_relations = near_duplicate_chunk_relations(chunks)
    print(f"Hierarchy rebuilt: parents={len(parents)}, segments={len(segments)}, chunks={len(chunks)}", flush=True)

    # Queue separation: one professional warning never contaminates data quality.
    data_quality: list[dict[str, Any]] = []
    regulation_validity: list[dict[str, Any]] = []
    source_traceability: list[dict[str, Any]] = []
    for parent in parents:
        reasons = data_quality_reasons(parent)
        if reasons:
            data_quality.append({**{k: v for k, v in parent.items() if not k.startswith("_")}, "review_reasons": reasons})
        if parent["parent_document_type"] == "regulation":
            regulation_validity.append({"document_id": parent["document_id"], "parent_document_id": parent["parent_document_id"], "parent_title": parent["parent_title"], "source_file": parent["source_file"], "source_url": parent["source_url"], "validity_status": "unknown", "review_reasons": ["regulation_validity_unknown"], "review_status": "pending"})
        source_reasons = []
        if not parent.get("source_url"): source_reasons.append("source_url_missing")
        if source_reasons:
            source_traceability.append({"document_id": parent["document_id"], "parent_document_id": parent["parent_document_id"], "parent_title": parent["parent_title"], "source_file": parent["source_file"], "source_url": parent["source_url"], "review_reasons": source_reasons, "review_status": "pending"})
    professional: list[dict[str, Any]] = []
    for segment in segments:
        if not segment.get("rag_eligible"):
            continue
        reasons = professional_review_reasons(segment["content"])
        if reasons:
            professional.append({"document_id": segment["document_id"], "parent_document_id": segment["parent_document_id"], "display_title": segment["display_title"], "source_file": segment["source_file"], "source_url": segment["source_url"], "review_reasons": reasons, "review_status": "pending"})

    validation = validate_all(parents, segments, chunks)
    metrics = rag_metrics(chunks, parents, segments, exact_chunks_removed, near_chunk_relations)

    clean_parents = [{k: v for k, v in row.items() if not k.startswith("_")} for row in parents]
    dump_jsonl(cleaned / "parent_documents.jsonl", clean_parents)
    dump_jsonl(cleaned / "segment_records.jsonl", segments)
    dump_jsonl(cleaned / "canonical_segments.jsonl", [row for row in segments if row["is_canonical"]])
    dump_jsonl(cleaned / "duplicate_segment_relations.jsonl", duplicate_relations)
    dump_jsonl(cleaned / "rag_chunks.jsonl", chunks)
    dump_jsonl(cleaned / "near_duplicate_chunk_relations.jsonl", near_chunk_relations)
    type_files = {
        "enterprise_plans.jsonl": {"enterprise_plan"}, "enterprise_group_plans.jsonl": {"enterprise_group_plan"},
        "government_plans.jsonl": {"government_or_regional_plan"}, "organization_plans.jsonl": {"organization_plan"},
        "sds_records.jsonl": {"sds"}, "regulations.jsonl": {"regulation"},
        "guidance_references.jsonl": {"guidance_reference"}, "accident_cases.jsonl": {"accident_case"},
        "excluded_irrelevant.jsonl": {"excluded_irrelevant"}, "rejected_records.jsonl": {"reject"},
    }
    for filename, types in type_files.items():
        dump_jsonl(cleaned / filename, [row for row in clean_parents if row["parent_document_type"] in types])
    dump_jsonl(cleaned / "data_quality_high_risk.jsonl", data_quality)
    dump_jsonl(cleaned / "professional_content_review.jsonl", professional)
    dump_jsonl(cleaned / "regulation_validity_review.jsonl", regulation_validity)
    dump_jsonl(cleaned / "source_traceability_review.jsonl", source_traceability)
    dump_json(cleaned / "superseded_rag_manifest.json", {"old_rag_file": str(root / "cleaned_full_v1" / "rag_chunks.jsonl"), "old_chunk_count": sum(1 for _ in (root / "cleaned_full_v1" / "rag_chunks.jsonl").open()), "status": "superseded", "database_imported": False, "embeddings_generated": False})
    dump_json(checkpoints / "pydantic_validation.json", validation)

    # Main reports and complete type review packages.
    changes = [row for row in parents if row["old_parent_document_type"] != row["parent_document_type"]]
    change_table = markdown_table(["document_id", "source_file", "source_url", "原类型", "新类型", "原置信度", "新置信度", "证据", "原因", "数据质量审核"], [
        (row["document_id"], row["source_file"], row["source_url"], row["old_parent_document_type"], row["parent_document_type"], row["old_classification_confidence"], row["classification_confidence"], "; ".join(row["classification_evidence"]), "; ".join(row["classification_reasons"]), bool(data_quality_reasons(row))) for row in changes
    ])
    write_md(reports / "parent_reclassification_changes.md", "父文档重新分类变化", [f"变化数：{len(changes)}", change_table])
    classification_review_reports(reports, parents)

    exact_rel = sum(row["duplicate_reason"] == "exact_normalized_content" for row in duplicate_relations)
    near_rel = sum(row["duplicate_reason"] == "near_rec_seg_layer_duplicate" for row in duplicate_relations)
    write_md(reports / "segment_layer_dedup_report.md", "Segment层去重报告", [
        f"- rec/seg完全重复关系：{exact_rel}\n- rec/seg近似重复关系：{near_rel}\n- 被排除出RAG的重复segment：{len(duplicate_relations)}\n- 保留追溯关系：{len(duplicate_relations)}\n- 同一正文以rec和seg双份进入RAG：{metrics['rec_seg_duplicate_chunks']}"
    ])
    full_segments = [row for row in segments if row["segment_type"] == "full_document"]
    full_ratio = len(full_segments) / max(1, len(segments))
    write_md(reports / "full_document_distribution_report.md", "full_document分布报告", [
        f"- full_document总数：{len(full_segments)}\n- 全部segment：{len(segments)}\n- 占比：{full_ratio:.2%}\n- 是否超过80%：{'是，需人工解释' if full_ratio > .8 else '否'}",
        "## 按父类型\n\n" + report_counts_table(Counter(row["parent_document_type"] for row in full_segments)),
        "## 按文件格式\n\n" + report_counts_table(Counter(Path(row["source_file"]).suffix.lower() or "(无扩展名)" for row in full_segments)),
        "## 按解析器\n\n" + report_counts_table(Counter(parent_map[row["parent_document_id"]]["parser_selected"] for row in full_segments)),
        f"无法继续切分原因：完整父文件保留一条追溯segment；有可靠结构章节时，该full_document不进入RAG。rec/seg去重共减少RAG候选segment {len(duplicate_relations)} 条。",
    ])
    write_md(reports / "title_inheritance_report.md", "标题继承报告", [
        f"- 父文档：{len(parents)}\n- 父标题缺失：{sum(not row['parent_title'] for row in parents)}\n- segment缺少继承父标题：{sum(not row['parent_title'] for row in segments)}\n- chunk缺少父标题：{metrics['without_parent_title']}\n- chunk缺少显示标题：{metrics['without_display_title']}\n\n显示规则：有章节标题时使用“父标题｜章节标题”，否则使用父标题。segment无独立章节标题不会触发missing_title。"
    ])
    render_queue_report(reports / "data_quality_high_risk.md", "数据质量高风险审核", data_quality)
    render_queue_report(reports / "professional_content_review.md", "专业内容审核", professional)
    render_queue_report(reports / "regulation_validity_review.md", "法规有效性审核", regulation_validity)
    render_queue_report(reports / "source_traceability_review.md", "来源可追溯审核", source_traceability)

    html_counts = Counter(row["page_type"] for row in html_report_rows)
    write_md(reports / "html_fallback_reprocess_report.md", "HTML fallback定点补跑报告", [
        f"- fallback记录：{len(html_report_rows)}\n- 成功恢复正文：{sum(row['parse_status']=='success' for row in html_report_rows)}\n- 登录页：{html_counts['login_page']}\n- 404：{html_counts['404']}\n- 空壳网页：{html_counts['empty_shell']}\n- excluded_irrelevant：{sum(parent_map.get(row['document_id'], {}).get('parent_document_type')=='excluded_irrelevant' for row in html_report_rows)}\n- 仍失败：{sum(row['parse_status']!='success' for row in html_report_rows)}",
        markdown_table(["document_id", "文件", "原解析器", "新解析器", "页面类型", "状态", "正文长度"], [(row["document_id"], row["source_file"], row["old_parser"], row["new_parser"], row["page_type"], row["parse_status"], row["text_length"]) for row in html_report_rows]),
    ])
    write_md(reports / "failed_pdf_reprocess_report.md", "失败PDF定点补跑报告", [
        f"失败PDF目标：{len(pdf_report_rows)}；可恢复：{sum(row['recoverable'] for row in pdf_report_rows)}；OCR使用：{sum(row['ocr_used'] for row in pdf_report_rows)}；仍失败：{sum(not row['recoverable'] for row in pdf_report_rows)}。",
        markdown_table(["文件", "magic bytes", "MIME", "原解析器", "回退解析器", "OCR", "结果", "可恢复", "最终状态"], [(row["source_file"], row["magic_bytes"], row["detected_mime_type"], row["original_parser"], row["fallback_parser"], row["ocr_used"], row["result"], row["recoverable"], row["final_status"]) for row in pdf_report_rows]),
    ])

    write_md(reports / "rag_chunk_quality_report.md", "RAG分块质量报告", [
        "## 总体指标\n\n" + markdown_table(["指标", "值"], [(key, json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value) for key, value in metrics.items() if not isinstance(value, dict)]),
        "## 按父类型\n\n" + report_counts_table(metrics["by_parent_type"]),
        "## 按segment类型\n\n" + report_counts_table(metrics["by_segment_type"]),
    ])

    parent_sample = stratified_sample(parents, "parent_document_type", 50)
    segment_sample = stratified_sample([row for row in segments if row["is_canonical"]], "segment_type", 50)
    chunk_sample = stratified_sample(chunks, "parent_document_type", 100)
    review_package(reports / "parent_sample_50.md", "父文档分层抽查50条", parent_sample, "parent")
    review_package(reports / "segment_sample_50.md", "Segment分层抽查50条", segment_sample, "segment")
    review_package(reports / "rag_chunk_sample_100.md", "RAG Chunk分层抽查100条", chunk_sample, "chunk")

    raw_ok, raw_changes, raw_hash_checked = raw_unchanged(old_manifest)
    ignored_system_hash_drift = []
    for row in system_rows:
        if not row.get("sha256"):
            continue
        try:
            actual = sha256_file(Path(row["source_file"]))
        except Exception:
            continue
        if actual != row["sha256"]:
            ignored_system_hash_drift.append({"source_file": row["source_file"], "expected": row["sha256"], "actual": actual, "status": "ignored_system_file_metadata_drift"})
    dump_json(checkpoints / "raw_integrity_check.json", {"unchanged": raw_ok, "hash_checked_non_system": raw_hash_checked, "content_unavailable_unhashed": sum(not row.get("sha256") for row in old_manifest), "changes": raw_changes, "ignored_system_file_hash_drift": ignored_system_hash_drift})
    pytest_result = run_pytest(root)
    dump_json(checkpoints / "pytest_result.json", pytest_result)

    # Formal acceptance checks.  They are data-driven and never use fixture IDs.
    enterprise_rows = [row for row in parents if row["parent_document_type"] in {"enterprise_plan", "enterprise_group_plan"}]
    acceptance = {
        "system_files_parent_count_zero": all(row["document_id"] not in {item["document_id"] for item in system_rows} for row in parents),
        "enterprise_independent_sds_zero": all(classify_parent_repaired(text=row["_text"], title=row["parent_title"], source_file=row["source_file"], parse_status=row["parse_status"])["parent_document_type"] != "sds" for row in enterprise_rows),
        "enterprise_accident_report_zero": all(not re.search(r"事故调查报告|事故通报", row["parent_title"]) for row in enterprise_rows),
        "enterprise_government_plan_zero": all(not re.search(r"街道办事处|人民政府|管委会|安委会", row["parent_title"] + Path(row["source_file"]).stem) for row in enterprise_rows),
        "sds_supplier_not_enterprise_evidence": all("concrete_enterprise_subject" not in row["classification_evidence"] for row in parents if row["parent_document_type"] == "sds"),
        "irrelevant_not_in_rag": all(row["parent_document_type"] != "excluded_irrelevant" for row in chunks),
        "height_not_confined_space": all(not (re.search(r"高空作业|高处作业|高处坠落", row["content"]) and row["accident_category"] == "confined_space") for row in chunks),
        "rec_seg_duplicate_chunks_zero": metrics["rec_seg_duplicate_chunks"] == 0,
        "rag_without_parent_title_zero": metrics["without_parent_title"] == 0,
        "rag_empty_chunks_zero": metrics["empty_chunks"] == 0,
        "data_quality_not_all_parents": len(data_quality) < len(parents),
        "professional_review_separated": bool(professional) and not any(set(row["review_reasons"]) == {"professional_response_content_requires_review"} for row in data_quality),
        "failed_parse_not_in_rag": all(parent_map[row["parent_document_id"]]["parse_status"] == "success" for row in chunks),
        "excluded_irrelevant_not_in_rag": all(parent_map[row["parent_document_id"]]["parent_document_type"] != "excluded_irrelevant" for row in chunks),
        "pydantic_all_passed": validation["failure_count"] == 0,
        "pytest_all_passed": pytest_result["passed"],
        "raw_sha256_unchanged": raw_ok,
        "all_results_pending": all(row["review_status"] == "pending" for row in [*parents, *segments, *chunks, *data_quality, *professional, *regulation_validity, *source_traceability]),
        "embeddings_not_generated": True,
        "database_not_imported": True,
    }
    passed = all(acceptance.values())
    dump_json(checkpoints / "acceptance_checks.json", {"passed": passed, "checks": acceptance})

    parent_counts = Counter(row["parent_document_type"] for row in parents)
    segment_counts = Counter(row["segment_type"] for row in segments)
    write_md(reports / "repaired_full_run_report.md", "第一次全量清洗定点修复运行报告", [
        f"- 执行时间：{now()}\n- 原始manifest：{len(old_manifest)}\n- 忽略系统文件：{len(system_rows)}\n- 父文档：{len(parents)}\n- segment：{len(segments)}\n- canonical segment：{sum(row['is_canonical'] for row in segments)}\n- 新RAG chunk：{len(chunks)}\n- 旧RAG chunk：56,964（状态：superseded）\n- HTML定点补跑：{len(html_targets)}\n- 失败PDF定点补跑：{len(pdf_targets)}\n- 未重新解析正常文件：是\n- 数据库导入：否\n- Embedding生成：否\n- 所有审核状态：pending",
        "## 父类型分布\n\n" + report_counts_table(parent_counts),
        "## Segment类型分布\n\n" + report_counts_table(segment_counts),
        f"## 四类审核队列\n\n- 数据质量：{len(data_quality)}\n- 专业内容：{len(professional)}\n- 法规有效性：{len(regulation_validity)}\n- 来源追溯：{len(source_traceability)}",
        "## 验收条件\n\n" + markdown_table(["条件", "结果"], [(key, "通过" if value else "失败") for key, value in acceptance.items()]),
        f"## 结论\n\n{'全部20项验收条件通过。' if passed else '存在未通过条件，不得进入数据库或生成Embedding。'}",
    ])
    print(f"Repair finished. acceptance={passed}, pytest={pytest_result['passed']}, raw_unchanged={raw_ok}", flush=True)
    return 0 if passed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.raw.is_dir():
        raise SystemExit(f"raw directory missing: {args.raw}")
    return run_repair(args.root.resolve(), args.raw.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
