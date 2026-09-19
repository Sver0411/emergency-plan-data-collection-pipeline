"""V2.4.2 fixed-sample runner.

This runner consumes the locked V2.1 manifest and the V2.4.1 isolated result
set. Parsing/deduplication provenance is retained; only the explicitly
requested child classification, title, and audit fields are recalculated.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .classification_v242 import (
    accident_category_v242,
    is_concrete_accident_case,
    lock_parent_type_v241,
    recover_title_v242,
    segment_type_v242,
)
from .config import PILOT_SEED, PILOT_SIZE, SOURCE_RECORDS
from .knowledge_extractor import extract_knowledge
from .models import PlanSection
from .pilot_runner_v2_4 import dump, locked_rows, parent_context, parent_id, record_id
from .section_splitter_v24 import split_sections_v24
from .title_audit_v242 import audit_title_consistency_v242, title_checks


ROOT = SOURCE_RECORDS.parent.parent
BASE_RESULTS = ROOT / "cleaned_v2_4_1" / "pilot_100_results_v2_4_1.json"
CLEANED = ROOT / "cleaned_v2_4_2"
REPORTS = ROOT / "reports_v2_4_2"

ALLOWED_SEGMENTS = {
    "full_document", "embedded_special_section", "onsite_disposal_plan",
    "guidance_section", "accident_case_section", "reference_sentence",
    "list_fragment", "toc_fragment", "table", "appendix_sds", "embedded_sds",
}


def _markdown(path: Path, heading: str, records: list[dict], *, kind: str = "segment") -> None:
    lines = [f"# {heading}", "", "本包仅供人工复核；自动结果和审核状态均为 pending。", ""]
    for index, item in enumerate(records, 1):
        lines.extend([f"## {index}. {item.get('document_id') or item.get('parent_document_id')}", ""])
        lines.extend([
            f"- 文件：{Path(item.get('source_file', '')).name}",
            f"- 来源URL：{item.get('source_url') or '缺失'}",
            f"- 父文档：{item.get('parent_document_id', '')}",
            f"- 父类型：{item.get('parent_document_type', '')}",
        ])
        if kind == "parent":
            lines.extend([
                f"- 父类型锁定：{item.get('parent_type_locked')}",
                f"- 证据范围：{item.get('parent_type_evidence_scope')}",
                f"- 分类证据：{json.dumps(item.get('classification_evidence', []), ensure_ascii=False)}",
                "- 人工父类型：________",
            ])
        else:
            lines.extend([
                f"- 子章节类型：{item.get('segment_type', '')}",
                f"- 事故类别：{item.get('accident_category', '')}",
                f"- 标题：{item.get('title', '')}",
                f"- 原标题：{item.get('original_title', '')}",
                f"- 标题状态/来源：{item.get('title_status', '')} / {item.get('title_source', '')}",
                f"- 标题正文一致性：{(item.get('title_consistency') or {}).get('status', '')}",
                f"- 标题检查：{json.dumps((item.get('title_consistency') or {}).get('checks', {}), ensure_ascii=False)}",
                f"- 警告：{item.get('warnings', [])}",
                "- 人工子章节类型：________",
                "- 人工标题：________",
                "- 边界是否正确：________",
            ])
        lines.extend(["- 人工备注：________", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _parent_reviews(output: list[dict], locks: dict[str, object], previous: dict[str, str]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in output:
        grouped[item["parent_document_id"]].append(item)
    records: list[dict] = []
    for parent, values in sorted(grouped.items()):
        representative = max(values, key=lambda item: len(item.get("normalized_text", "")))
        lock = locks[parent]
        records.append({
            "parent_document_id": parent,
            "representative_document_id": representative["document_id"],
            "file_name": representative.get("source_file", ""),
            "source_url": representative.get("source_url", ""),
            "previous_parent_document_type": previous.get(parent, ""),
            "parent_document_type": lock.result.parent_document_type,
            "content_role": lock.result.content_role,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "classification_evidence": lock.result.classification_evidence,
            "classification_reasons": lock.result.classification_reasons,
            "classification_confidence": lock.result.classification_confidence,
            "requires_manual_review": lock.result.requires_manual_review,
            "review_status": "pending",
        })
    return records


def _set_pending(value):
    if isinstance(value, dict):
        for key in list(value):
            if key == "review_status":
                value[key] = "pending"
            else:
                _set_pending(value[key])
    elif isinstance(value, list):
        for item in value:
            _set_pending(item)


def _old_parent_types(base_rows: list[dict]) -> dict[str, str]:
    counters: dict[str, Counter] = defaultdict(Counter)
    for item in base_rows:
        counters[item["parent_document_id"]][item.get("parent_document_type", "")] += 1
    return {parent: count.most_common(1)[0][0] for parent, count in counters.items() if count}


def main() -> None:
    CLEANED.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    rows = json.loads(SOURCE_RECORDS.read_text(encoding="utf-8"))
    selected = locked_rows(rows)
    if len(selected) != PILOT_SIZE:
        raise RuntimeError(f"locked sample changed: {len(selected)}")
    base_rows = json.loads(BASE_RESULTS.read_text(encoding="utf-8"))
    base_by_id = {item["document_id"]: item for item in base_rows}
    locked_ids = {record_id(index, row) for index, row, _ in selected}
    base_ids = {ident for ident in base_by_id if not ident.endswith("-appendix-sds")}
    if locked_ids != base_ids:
        raise RuntimeError("V2.4.2 input is not the same locked 100-record set")

    groups: dict[str, list[tuple[int, dict, str]]] = defaultdict(list)
    for item in selected:
        groups[parent_id(item[1])].append(item)
    contexts: dict[str, tuple[str, str, str]] = {}
    locks = {}
    for parent, items in groups.items():
        context = parent_context(items, parent)
        contexts[parent] = context
        locks[parent] = lock_parent_type_v241(parent, *context)

    old_types = _old_parent_types(base_rows)
    output: list[dict] = []
    manual_queue: list[dict] = []
    for index, row, bucket in selected:
        ident = record_id(index, row)
        base = dict(base_by_id[ident])
        parent = parent_id(row)
        lock = locks[parent]
        _, parent_text, _ = contexts[parent]
        decision = recover_title_v242(
            base.get("original_title", row.get("title", "")),
            base.get("title", ""),
            base.get("normalized_text") or base.get("raw_text", ""),
            parent_text,
            base.get("source_file", row.get("file", "")),
            base.get("segment_type", "full_document"),
            lock.result.parent_document_type,
        )
        title = str(decision["title"])
        status = str(decision["status"])
        normalized = base.get("normalized_text") or base.get("raw_text", "")
        segment = segment_type_v242(
            title, normalized, lock.result.parent_document_type, len(groups[parent]),
            status, base.get("original_title", ""), base.get("segment_type", "full_document"),
        )
        # A government attachment title inside a notification represents the
        # complete parent document, not a child embedded section.
        if (
            segment == "embedded_special_section"
            and decision["source"] == "attachment_title"
            and lock.result.parent_document_type == "government_or_regional_plan"
            and re.search(r"关于发布|通知|附件", parent_text[:2500])
        ):
            segment = "full_document"
        category = (
            "other"
            if lock.result.parent_document_type in {"sds", "chemical_catalog", "regulation"}
            else accident_category_v242(title, normalized, segment, base.get("accident_category", "other"))
        )
        sections = [] if segment in {"toc_fragment", "list_fragment", "reference_sentence"} else [
            section.model_dump() for section in split_sections_v24(normalized, category)
        ]
        consistency = audit_title_consistency_v242(
            document_id=ident, title=title, text=normalized, segment_type=segment,
            title_status=status, title_source=str(decision["source"]), sections=sections,
        )
        warnings = [warning for warning in base.get("warnings", []) if warning not in {
            "sentence_used_as_title", "boundary_error", "title_body_mismatch",
            "cross_special_plan_contamination", "title_recovered_from_parent_parse",
        }]
        warnings.extend(decision["warnings"])
        checks = title_checks(title, normalized, status, str(decision["source"]), segment)
        for key, value in checks.items():
            if value and key != "full_document_boundary_not_applicable":
                warnings.append(key)
        if consistency["status"] == "inconsistent":
            warnings.append("title_body_inconsistent")
        if consistency["status"] == "uncertain":
            warnings.append("title_body_uncertain")
        warnings = list(dict.fromkeys(warnings))
        requires_review = bool(
            lock.result.requires_manual_review or status != "valid"
            or consistency["status"] in {"inconsistent", "uncertain"}
            or segment in {"toc_fragment", "list_fragment", "reference_sentence"} or warnings
        )
        role = lock.result.content_role
        if segment == "accident_case_section":
            role = "accident_case_reference"
        elif segment == "guidance_section":
            role = "guidance_section"
        elif segment == "onsite_disposal_plan":
            role = "disposal_knowledge"
        elif segment == "reference_sentence":
            role = "reference_sentence"
        elif segment == "list_fragment":
            role = "list_reference"
        elif segment == "toc_fragment":
            role = "toc_reference"
        elif segment in {"appendix_sds", "embedded_sds"}:
            role = "safety_data"
        record = dict(base)
        record.update({
            "title": title,
            "title_status": status,
            "title_source": decision["source"],
            "title_recovery_evidence": decision["evidence"],
            "parent_document_type": lock.result.parent_document_type,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "segment_type": segment,
            "accident_category": category,
            "content_role": role,
            "classification_reasons": lock.result.classification_reasons,
            "classification_evidence": lock.result.classification_evidence,
            "classification_confidence": lock.result.classification_confidence,
            "requires_manual_review": requires_review,
            "sections": sections,
            "knowledge_items": [item.model_dump() for item in extract_knowledge(
                ident, base.get("source_url", ""),
                [PlanSection.model_validate(section) for section in sections],
            )],
            "warnings": warnings,
            "quality_grade": "manual_review" if requires_review else base.get("quality_grade", "silver_candidate"),
            "review_status": "pending",
            "title_consistency": consistency,
        })
        _set_pending(record)
        output.append(record)
        if requires_review:
            manual_queue.append({
                "document_id": ident,
                "parent_document_id": parent,
                "stage": "v2_4_2_title_segment_review",
                "reason": list(dict.fromkeys(warnings + consistency["reasons"])),
                "review_status": "pending",
            })

    # Preserve the previously validated SDS attachment record and parent lock.
    for base in base_rows:
        if not base["document_id"].endswith("-appendix-sds"):
            continue
        parent = base["parent_document_id"]
        lock = locks[parent]
        record = dict(base)
        record.update({
            "parent_document_type": lock.result.parent_document_type,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "segment_type": "appendix_sds",
            "review_status": "pending",
            "title_status": "valid",
            "title_source": "derived_appendix_title",
        })
        record["title_consistency"] = audit_title_consistency_v242(
            document_id=record["document_id"], title=record.get("title", "SDS附件"),
            text=record.get("normalized_text", ""), segment_type="appendix_sds",
            title_status="valid", title_source="derived_appendix_title",
            sections=record.get("sections", []),
        )
        _set_pending(record)
        output.append(record)

    parent_records = _parent_reviews(output, locks, old_types)
    parent_changes = [item for item in parent_records if item["previous_parent_document_type"] != item["parent_document_type"]]
    rejected = [item for item in output if item.get("parent_document_type") == "reject" or item.get("quality_grade") == "rejected"]
    dump(CLEANED / "pilot_100_results_v2_4_2.json", output)
    dump(CLEANED / "parent_documents_v2_4_2.json", parent_records)
    dump(CLEANED / "segment_records_v2_4_2.json", output)
    dump(CLEANED / "manual_review_queue_v2_4_2.json", manual_queue)
    dump(CLEANED / "rejected_records_v2_4_2.json", rejected)

    by_type = {
        "accident_case_sections": [item for item in output if item["segment_type"] == "accident_case_section"],
        "toc_fragments": [item for item in output if item["segment_type"] == "toc_fragment"],
        "reference_sentences": [item for item in output if item["segment_type"] == "reference_sentence"],
        "list_fragments": [item for item in output if item["segment_type"] == "list_fragment"],
        "guidance_sections": [item for item in output if item["segment_type"] == "guidance_section"],
        "onsite_disposal_plans": [item for item in output if item["segment_type"] == "onsite_disposal_plan"],
        "valid_titles": [item for item in output if item["title_status"] == "valid"],
        "invalid_uncertain_titles": [item for item in output if item["title_status"] in {"invalid", "uncertain", "missing", "toc_only"}],
    }
    for key, records in by_type.items():
        dump(REPORTS / f"manual_review_{key}_v2_4_2.json", records)
        _markdown(REPORTS / f"manual_review_{key}_v2_4_2.md", f"V2.4.2 {key}审核包", records)
    review30 = sorted(output, key=lambda item: ((item.get("title_consistency") or {}).get("status") not in {"inconsistent", "uncertain"}, item["document_id"]))[:30]
    dump(REPORTS / "manual_review_segments_30_v2_4_2.json", review30)
    _markdown(REPORTS / "manual_review_segments_30_v2_4_2.md", "V2.4.2子章节审核包（30条）", review30)
    dump(REPORTS / "manual_review_all_parents_49_v2_4_2.json", parent_records)
    _markdown(REPORTS / "manual_review_all_parents_49_v2_4_2.md", "V2.4.2全部父文档审核包", parent_records, kind="parent")

    title_audit = []
    consistency_audit = []
    for item in output:
        checks = title_checks(item.get("title", ""), item.get("normalized_text", ""), item.get("title_status", ""), item.get("title_source", ""), item.get("segment_type", ""))
        title_audit.append({
            "document_id": item["document_id"], "parent_document_id": item["parent_document_id"],
            "original_title": item.get("original_title", ""), "title": item.get("title", ""),
            "title_status": item.get("title_status", ""), "title_source": item.get("title_source", ""),
            "title_recovery_evidence": item.get("title_recovery_evidence", []),
            "checks": checks, "review_status": "pending",
        })
        consistency_audit.append(item["title_consistency"])
    dump(REPORTS / "title_recovery_audit_v2_4_2.json", title_audit)
    dump(REPORTS / "title_body_consistency_v2_4_2.json", consistency_audit)
    (REPORTS / "title_recovery_audit_v2_4_2.md").write_text(
        "# V2.4.2标题恢复审计\n\n逐条保留标题来源和硬性校验；不得凭空生成标题，全部结果保持 pending。\n\n"
        + "\n".join(
            f"- {item['document_id']}｜{item['title_status']}｜{item['title_source']}｜{item['title']}"
            for item in title_audit if item["title_status"] != "valid" or any(item["checks"].values())
        ) + "\n", encoding="utf-8",
    )
    consistency_counts = Counter(item["status"] for item in consistency_audit)
    (REPORTS / "title_body_consistency_v2_4_2.md").write_text(
        "# V2.4.2标题正文一致性报告\n\n"
        f"- 状态统计：{dict(consistency_counts)}\n"
        "- 检查标题来源、标题事故类别、正文前500字、首个章节、目录/通知粘连、子章节结构和跨专项混入。\n"
        "- 自动状态不是人工验收结论，全部保持 pending。\n\n"
        + "\n".join(
            f"- {item['document_id']}｜{item['status']}｜{item['reasons']}"
            for item in consistency_audit if item["status"] in {"inconsistent", "uncertain"}
        ) + "\n", encoding="utf-8",
    )

    old_by_id = {item["document_id"]: item for item in base_rows}
    changes = []
    for item in output:
        old = old_by_id.get(item["document_id"], {})
        before = {key: old.get(key, "") for key in ("segment_type", "accident_category", "title", "title_status")}
        after = {key: item.get(key, "") for key in ("segment_type", "accident_category", "title", "title_status")}
        if before != after:
            changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before": before, "after": after, "review_status": "pending"})
    dump(REPORTS / "segment_type_changes_v2_4_2.json", changes)
    (REPORTS / "segment_type_changes_v2_4_2.md").write_text(
        "# V2.4.2子章节类型变化报告\n\n"
        f"- 变化记录：{len(changes)}条。\n\n"
        + "\n".join(f"- {item['document_id']}｜{item['before']} → {item['after']}" for item in changes) + "\n",
        encoding="utf-8",
    )

    segment_counts = Counter(item["segment_type"] for item in output)
    title_counts = Counter(item["title_status"] for item in output)
    valid_checks = [title_checks(item.get("title", ""), item.get("normalized_text", ""), item.get("title_status", ""), item.get("title_source", ""), item.get("segment_type", "")) for item in output if item.get("title_status") == "valid"]
    forbidden_valid = {
        "over_80_chars_valid": sum(check["over_80_chars"] for check in valid_checks),
        "toc_dots_valid": sum(check["contains_toc_dots"] for check in valid_checks),
        "body_sentence_valid": sum(check["contains_body_sentence"] for check in valid_checks),
        "multiple_toc_entries_valid": sum(check["multiple_toc_entries"] for check in valid_checks),
        "notification_fulltext_valid": sum(check["notification_fulltext"] for check in valid_checks),
        "publication_note_valid": sum(check["publication_note"] for check in valid_checks),
        "first_aid_sentence_valid": sum(check["first_aid_sentence"] for check in valid_checks),
    }
    repair_stats = {
        "guidance_case_false_positive_fixed": sum(old_by_id.get(item["document_id"], {}).get("segment_type") == "accident_case_section" and item["segment_type"] != "accident_case_section" and item["parent_document_type"] == "guidance_reference" and not is_concrete_accident_case(item.get("normalized_text", ""), item.get("title", "")) for item in output),
        "toc_list_false_positive_fixed": sum(old_by_id.get(item["document_id"], {}).get("segment_type") == "list_fragment" and item["segment_type"] == "toc_fragment" for item in output),
        "onsite_reference_false_positive_fixed": sum(old_by_id.get(item["document_id"], {}).get("segment_type") == "reference_sentence" and item["segment_type"] == "onsite_disposal_plan" for item in output),
        "guidance_list_false_positive_fixed": sum(old_by_id.get(item["document_id"], {}).get("segment_type") == "list_fragment" and item["segment_type"] == "guidance_section" for item in output),
        "notification_title_fixed": sum(len(old_by_id.get(item["document_id"], {}).get("title", "")) > 80 and len(item.get("title", "")) <= 80 for item in output),
        "regulation_publication_title_fixed": sum(old_by_id.get(item["document_id"], {}).get("title", "") != item.get("title", "") and item.get("parent_document_type") == "regulation" for item in output),
        "sds_first_aid_title_fixed": sum(bool(re.search(r"需要立即就医|向现场的医生", old_by_id.get(item["document_id"], {}).get("title", ""))) and item.get("title_status") != "invalid" for item in output),
        "full_document_boundary_warning_removed": sum("boundary_error" in old_by_id.get(item["document_id"], {}).get("warnings", []) and item.get("segment_type") == "full_document" and "boundary_error" not in item.get("warnings", []) for item in output),
    }
    report = [
        "# V2.4.2固定100条试运行报告", "",
        "## 输入统计", "",
        f"- 输入记录数：{len(selected)}", f"- 结构化记录数：{len(output)}",
        f"- 独立父文档数：{len(locks)}", f"- 固定种子：{PILOT_SEED}",
        "- 是否重新抽样：否，使用 V2.1 以来同一锁定清单。",
        "- 解析说明：复用已审计的 V2.4.1解析/标准化/去重产物，仅重新计算本轮定点字段。", "",
        "## 子章节统计", "",
    ]
    report.extend(f"- {key}：{segment_counts.get(key, 0)}" for key in sorted(ALLOWED_SEGMENTS))
    report.extend(["", "## 标题统计", ""])
    report.extend(f"- {key}：{title_counts.get(key, 0)}" for key in ("valid", "uncertain", "invalid", "toc_only", "missing"))
    report.extend([
        f"- valid标题超过80字：{forbidden_valid['over_80_chars_valid']}",
        f"- valid标题包含目录点线：{forbidden_valid['toc_dots_valid']}",
        f"- valid标题包含正文句：{forbidden_valid['body_sentence_valid']}",
        f"- valid标题包含多个目录条目：{forbidden_valid['multiple_toc_entries_valid']}",
        f"- 通知全文被当作valid标题：{forbidden_valid['notification_fulltext_valid']}",
        f"- 发布说明被当作valid标题：{forbidden_valid['publication_note_valid']}",
        f"- 急救正文被当作valid标题：{forbidden_valid['first_aid_sentence_valid']}", "", "## 一致性统计", "",
    ])
    report.extend(f"- {key}：{consistency_counts.get(key, 0)}" for key in ("consistent", "inconsistent", "uncertain", "not_applicable"))
    report.extend(["", "## 回归修复统计", ""])
    report.extend(f"- {key}：{value}" for key, value in repair_stats.items())
    report.extend([
        "", "## 审核统计", "", f"- 人工审核记录数量：{len(manual_queue)}",
        f"- 拒绝记录数量：{len(rejected)}",
        f"- 来源可追溯率：{sum(bool(item.get('source_url') or item.get('source_file')) for item in output) / len(output):.3f}",
        f"- 所有记录是否保持pending：{all(item.get('review_status') == 'pending' for item in output)}",
        f"- 父文档类型意外变化数量：{len(parent_changes)}",
        "", "## 验收说明", "",
        "- 本轮不把自动状态视为人工准确率，不自动批准任何记录。",
        "- 本轮未执行全量清洗、重新爬取或数据库/知识库导入。",
    ])
    (REPORTS / "pilot_100_report_v2_4_2.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    dump(REPORTS / "title_hard_check_metrics_v2_4_2.json", {"title_status_counts": dict(title_counts), "valid_title_forbidden_counts": forbidden_valid, "review_status": "pending"})

    pytest_file = REPORTS / "pytest_v2_4_2.txt"
    pytest_text = pytest_file.read_text(encoding="utf-8") if pytest_file.exists() else "pytest输出待写入"
    (REPORTS / "regression_test_report_v2_4_2.md").write_text(
        "# V2.4.2回归测试报告\n\n" + pytest_text.strip() + "\n\n"
        "- 新增回归测试覆盖：指导意见/事故案例证据、目录粘连、完整现场处置、完整指导章节、列表条目、通知/发布说明、SDS急救句、full_document边界、pending和固定样本。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
