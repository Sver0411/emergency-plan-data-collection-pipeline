from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from .classification_v241 import (
    accident_category_v241,
    lock_parent_type_v241,
    recover_title_v241,
    segment_type_v241,
    sentence_title_reasons,
)
from .config import PILOT_SEED, PILOT_SIZE, SOURCE_RECORDS
from .knowledge_extractor import extract_knowledge
from .models import PlanSection
from .pilot_runner_v2_4 import dump, locked_rows, parent_context, parent_id, record_id
from .section_splitter_v24 import split_sections_v24
from .title_audit_v241 import audit_title_consistency_v241


ROOT = SOURCE_RECORDS.parent.parent
CLEANED_V24 = ROOT / "cleaned_v2_4"
CLEANED_V241 = ROOT / "cleaned_v2_4_1"
REPORTS_V241 = ROOT / "reports_v2_4_1"
BASE_RESULTS = CLEANED_V24 / "pilot_100_results.json"


def _review_markdown(path: Path, heading: str, records: list[dict], *, parent: bool = False) -> None:
    lines = [f"# {heading}", "", "本审核包仅供人工复核；所有自动结果均保持 `pending`。", ""]
    for index, item in enumerate(records, 1):
        lines.extend([f"## {index}. {item.get('document_id') or item.get('parent_document_id')}", ""])
        if parent:
            lines.extend([
                f"- 文件：{item.get('file_name', '')}",
                f"- 来源：{item.get('source_url') or '缺失'}",
                f"- V2.4父类型：{item.get('previous_parent_document_type', '')}",
                f"- V2.4.1父类型：{item.get('parent_document_type')}",
                f"- 内容用途：{item.get('content_role')}",
                f"- 锁定证据范围：{item.get('parent_type_evidence_scope')}",
                f"- 分类证据：{json.dumps(item.get('classification_evidence', []), ensure_ascii=False)}",
                "- 人工父类型：________",
                "- 人工备注：________",
            ])
        else:
            lines.extend([
                f"- 父文档：{item.get('parent_document_id')}",
                f"- 父类型：{item.get('parent_document_type')}",
                f"- 子章节类型：{item.get('segment_type')}",
                f"- 事故类别：{item.get('accident_category')}",
                f"- 原标题：{item.get('original_title', '')}",
                f"- V2.4.1标题：{item.get('title', '')}",
                f"- 标题状态/来源：{item.get('title_status')} / {item.get('title_source')}",
                f"- 一致性：{item.get('title_consistency', {}).get('status', '')}",
                f"- 警告：{item.get('warnings', [])}",
                "- 人工子章节类型：________",
                "- 人工标题：________",
                "- 边界是否正确：________",
                "- 人工备注：________",
            ])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _parent_reviews(output: list[dict], locks: dict[str, object], previous_parent_types: dict[str, str]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in output:
        grouped[item["parent_document_id"]].append(item)
    reviews = []
    for parent, values in grouped.items():
        representative = max(values, key=lambda item: len(item.get("normalized_text", "")))
        lock = locks[parent]
        reviews.append({
            "parent_document_id": parent,
            "representative_document_id": representative["document_id"],
            "file_name": Path(representative["source_file"]).name,
            "source_url": representative["source_url"],
            "previous_parent_document_type": previous_parent_types.get(parent, ""),
            "parent_document_type": lock.result.parent_document_type,
            "content_role": lock.result.content_role,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "classification_evidence": lock.result.classification_evidence,
            "classification_confidence": lock.result.classification_confidence,
            "requires_manual_review": lock.result.requires_manual_review,
            "review_status": "pending",
        })
    return sorted(reviews, key=lambda item: item["parent_document_id"])


def main() -> None:
    CLEANED_V241.mkdir(exist_ok=True)
    REPORTS_V241.mkdir(exist_ok=True)
    rows = json.loads(SOURCE_RECORDS.read_text(encoding="utf-8"))
    selected = locked_rows(rows)
    if len(selected) != PILOT_SIZE:
        raise RuntimeError(f"locked sample changed: {len(selected)}")

    groups: dict[str, list[tuple[int, dict, str]]] = defaultdict(list)
    for item in selected:
        groups[parent_id(item[1])].append(item)

    contexts = {}
    locks = {}
    for parent, items in groups.items():
        source_title, parent_text, original_type = parent_context(items, parent)
        contexts[parent] = (source_title, parent_text, original_type)
        locks[parent] = lock_parent_type_v241(parent, source_title, parent_text, original_type)

    base_rows = json.loads(BASE_RESULTS.read_text(encoding="utf-8"))
    base_by_id = {item["document_id"]: item for item in base_rows}
    previous_parent_counts: dict[str, Counter] = defaultdict(Counter)
    for item in base_rows:
        previous_parent_counts[item["parent_document_id"]][item["parent_document_type"]] += 1
    previous_parent_types = {
        parent: counts.most_common(1)[0][0]
        for parent, counts in previous_parent_counts.items()
    }

    output: list[dict] = []
    manual_queue: list[dict] = []
    for index, row, bucket in selected:
        ident = record_id(index, row)
        parent = parent_id(row)
        base = base_by_id[ident]
        lock = locks[parent]
        _, parent_text, _ = contexts[parent]
        decision = recover_title_v241(
            base.get("original_title", row.get("title", "")),
            base.get("title", ""),
            base.get("normalized_text", ""),
            parent_text,
            base.get("source_file", row.get("file", "")),
            base.get("segment_type", "full_document"),
        )
        segment_type = segment_type_v241(
            decision.title,
            base.get("normalized_text", ""),
            lock.result.parent_document_type,
            len(groups[parent]),
            decision.status,
        )
        category = (
            "other"
            if lock.result.parent_document_type in {"sds", "chemical_catalog", "regulation"}
            else accident_category_v241(decision.title, base.get("normalized_text", ""), segment_type)
        )
        sections = [] if segment_type in {"list_fragment", "reference_sentence", "toc_fragment"} else [
            section.model_dump() for section in split_sections_v24(base.get("normalized_text", ""), category)
        ]
        consistency = audit_title_consistency_v241(
            document_id=ident,
            title=decision.title,
            text=base.get("normalized_text", ""),
            segment_type=segment_type,
            title_status=decision.status,
            title_source=decision.source,
            sections=sections,
        )
        warnings = [warning for warning in base.get("warnings", []) if warning not in {
            "sentence_used_as_title", "boundary_error", "title_body_mismatch", "cross_special_plan_contamination"
        }]
        warnings.extend(decision.warnings)
        if consistency["status"] in {"inconsistent", "uncertain"}:
            warnings.append("boundary_error")
        if "multiple_peer_special_headings" in consistency["reasons"]:
            warnings.append("cross_special_plan_contamination")
        warnings = list(dict.fromkeys(warnings))
        requires_review = bool(lock.result.requires_manual_review or decision.status != "valid" or consistency["status"] in {"inconsistent", "uncertain"} or warnings)
        role = lock.result.content_role
        if segment_type == "accident_case_section":
            role = "accident_case_reference"
        elif segment_type == "onsite_disposal_plan":
            role = "disposal_knowledge"
        elif segment_type == "reference_sentence":
            role = "reference_sentence"
        elif segment_type == "list_fragment":
            role = "list_reference"
        elif segment_type in {"appendix_sds", "embedded_sds"}:
            role = "safety_data"

        knowledge = [item.model_dump() for item in extract_knowledge(
            ident,
            base.get("source_url", ""),
            [PlanSection.model_validate(section) for section in sections],
        )]
        quality = "low_quality" if lock.result.parent_document_type == "sds" and lock.result.requires_manual_review else "manual_review" if requires_review else "silver_candidate"
        record = dict(base)
        record.update({
            "title": decision.title,
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
            "title_status": decision.status,
            "title_source": decision.source,
            "title_recovery_evidence": decision.evidence,
            "title_consistency": consistency,
            "requires_manual_review": requires_review,
            "sections": sections,
            "knowledge_items": knowledge,
            "warnings": warnings,
            "sampling_bucket": bucket,
            "quality_grade": quality,
            "review_status": "pending",
        })
        if record.get("sds"):
            record["sds"]["review_status"] = "pending"
        output.append(record)
        if requires_review:
            manual_queue.append({
                "document_id": ident,
                "parent_document_id": parent,
                "stage": "v2_4_1_title_or_segment_review",
                "reason": list(dict.fromkeys(warnings + consistency["reasons"])),
                "review_status": "pending",
            })

    # Preserve the V2.4 SDS attachment split as a derived child while applying
    # the newly locked parent type and V2.4.1 audit fields.
    for base in base_rows:
        if not base["document_id"].endswith("-appendix-sds"):
            continue
        parent = base["parent_document_id"]
        lock = locks[parent]
        record = dict(base)
        consistency = audit_title_consistency_v241(
            document_id=record["document_id"], title=record["title"], text=record["normalized_text"],
            segment_type="appendix_sds", title_status="valid", title_source="derived_appendix_title", sections=record.get("sections", []),
        )
        record.update({
            "parent_document_type": lock.result.parent_document_type,
            "parent_type_locked": True,
            "parent_type_evidence_scope": lock.evidence_scope,
            "embedded_content_types": lock.embedded_content_types,
            "segment_type": "appendix_sds",
            "title_status": "valid",
            "title_source": "derived_appendix_title",
            "title_recovery_evidence": ["V2.4已验证的SDS附件拆分边界"],
            "title_consistency": consistency,
            "review_status": "pending",
        })
        if record.get("sds"):
            record["sds"]["review_status"] = "pending"
        output.append(record)

    dump(CLEANED_V241 / "pilot_100_results_v2_4_1.json", output)
    dump(CLEANED_V241 / "pilot_100_manual_review_queue_v2_4_1.json", manual_queue)

    all_parents = _parent_reviews(output, locks, previous_parent_types)
    changed_parents = [item for item in all_parents if item["previous_parent_document_type"] != item["parent_document_type"]]
    dump(REPORTS_V241 / "manual_review_all_parents_49_v2_4_1.json", all_parents)
    dump(REPORTS_V241 / "manual_review_changed_parents_v2_4_1.json", changed_parents)
    _review_markdown(REPORTS_V241 / "manual_review_all_parents_49_v2_4_1.md", "V2.4.1全部49个父文档审核包", all_parents, parent=True)
    _review_markdown(REPORTS_V241 / "manual_review_changed_parents_v2_4_1.md", "V2.4.1父类型变化审核包", changed_parents, parent=True)

    accident_cases = [item for item in output if item["segment_type"] == "accident_case_section"]
    reference_and_lists = [item for item in output if item["segment_type"] in {"reference_sentence", "list_fragment"}]
    dump(REPORTS_V241 / "manual_review_accident_case_sections_v2_4_1.json", accident_cases)
    dump(REPORTS_V241 / "manual_review_reference_and_list_fragments_v2_4_1.json", reference_and_lists)
    _review_markdown(REPORTS_V241 / "manual_review_accident_case_sections_v2_4_1.md", "V2.4.1全部事故案例子章节审核包", accident_cases)
    _review_markdown(REPORTS_V241 / "manual_review_reference_and_list_fragments_v2_4_1.md", "V2.4.1引用句与列表片段审核包", reference_and_lists)

    candidates = sorted(output, key=lambda item: (item["title_consistency"]["status"] not in {"inconsistent", "uncertain"}, item["document_id"]))
    rng = random.Random(PILOT_SEED + 241)
    rng.shuffle(candidates)
    candidates.sort(key=lambda item: (item["title_consistency"]["status"] not in {"inconsistent", "uncertain"}, item["document_id"]))
    segment_review = candidates[:30]
    dump(REPORTS_V241 / "manual_review_segments_30_v2_4_1.json", segment_review)
    _review_markdown(REPORTS_V241 / "manual_review_segments_30_v2_4_1.md", "V2.4.1子章节审核包（30条）", segment_review)

    title_audit = [{
        "document_id": item["document_id"],
        "parent_document_id": item["parent_document_id"],
        "original_title": item.get("original_title", ""),
        "v2_4_title": base_by_id.get(item["document_id"], {}).get("title", ""),
        "v2_4_1_title": item["title"],
        "title_status": item["title_status"],
        "title_source": item["title_source"],
        "title_recovery_evidence": item["title_recovery_evidence"],
        "sentence_title_reasons": sentence_title_reasons(item["title"], has_heading_evidence=item["title_status"] == "valid"),
        "review_status": "pending",
    } for item in output]
    consistency_audit = [item["title_consistency"] for item in output]
    dump(REPORTS_V241 / "title_recovery_audit_v2_4_1.json", title_audit)
    dump(REPORTS_V241 / "title_body_consistency_v2_4_1.json", consistency_audit)

    title_lines = ["# V2.4.1标题恢复审计", "", "所有恢复结果均可追溯到原始标题、正文标题或文件名；无法可靠恢复的记录保持 `uncertain`。", ""]
    for item in title_audit:
        if item["v2_4_title"] != item["v2_4_1_title"] or item["title_status"] != "valid":
            title_lines.append(f"- `{item['document_id']}`｜V2.4：{item['v2_4_title']}｜V2.4.1：{item['v2_4_1_title']}｜{item['title_status']} / {item['title_source']}")
    (REPORTS_V241 / "title_recovery_audit_v2_4_1.md").write_text("\n".join(title_lines) + "\n", encoding="utf-8")

    consistency_counts = Counter(item["status"] for item in consistency_audit)
    consistency_lines = ["# V2.4.1标题正文一致性报告", "", f"- 状态统计：{dict(consistency_counts)}", "- 检查范围：标题事故类别、正文前500字、首个真实章节标题、同级专项标题、标题来源及主题一致性。", "- 自动状态不是人工验收结论，全部保持 `pending`。", ""]
    for item in consistency_audit:
        if item["status"] in {"inconsistent", "uncertain"}:
            consistency_lines.append(f"- `{item['document_id']}`｜`{item['status']}`｜{item['reasons']}")
    (REPORTS_V241 / "title_body_consistency_v2_4_1.md").write_text("\n".join(consistency_lines) + "\n", encoding="utf-8")

    base_map = {item["document_id"]: item for item in base_rows}
    segment_changes = []
    for item in output:
        old = base_map.get(item["document_id"], {})
        before = {key: old.get(key, "") for key in ("segment_type", "accident_category", "title")}
        after = {key: item.get(key, "") for key in ("segment_type", "accident_category", "title")}
        if before != after:
            segment_changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before": before, "after": after, "review_status": "pending"})
    dump(REPORTS_V241 / "segment_type_changes_v2_4_1.json", segment_changes)
    change_lines = ["# V2.4.1子章节类型变化报告", "", f"- 变化记录：{len(segment_changes)}条。", ""]
    for item in segment_changes:
        change_lines.append(f"- `{item['document_id']}`｜{item['before']} → {item['after']}")
    (REPORTS_V241 / "segment_type_changes_v2_4_1.md").write_text("\n".join(change_lines) + "\n", encoding="utf-8")

    parent_changes = [{
        "parent_document_id": item["parent_document_id"],
        "before": item["previous_parent_document_type"],
        "after": item["parent_document_type"],
        "content_role": item["content_role"],
        "review_status": "pending",
    } for item in changed_parents]
    dump(REPORTS_V241 / "parent_type_changes_v2_4_1.json", parent_changes)

    parent_counts = Counter(lock.result.parent_document_type for lock in locks.values())
    segment_counts = Counter(item["segment_type"] for item in output)
    invalid_title_count = sum(item["title_status"] != "valid" for item in output)
    valid_title_false_positive_count = sum(
        item["title_status"] == "valid" and "sentence_used_as_title" in item.get("warnings", [])
        for item in output
    )
    report = [
        "# V2.4.1固定100条试运行报告", "",
        f"固定种子：`{PILOT_SEED}`；沿用同一锁定清单，输入100条，未重新抽样。", "",
        "## 统计", "",
        f"- 结构化记录（含SDS附件）：{len(output)}",
        f"- 独立父文档：{len(locks)}",
        f"- government_or_regional_plan父文档数量：{parent_counts.get('government_or_regional_plan', 0)}",
        f"- guidance_reference父文档数量：{parent_counts.get('guidance_reference', 0)}",
        f"- accident_case_section数量：{segment_counts.get('accident_case_section', 0)}",
        f"- reference_sentence数量：{segment_counts.get('reference_sentence', 0)}",
        f"- list_fragment数量：{segment_counts.get('list_fragment', 0)}",
        f"- 无效或无法可靠恢复标题数量：{invalid_title_count}",
        f"- 合法标题被误报sentence_used_as_title数量：{valid_title_false_positive_count}",
        f"- 标题正文consistent数量：{consistency_counts.get('consistent', 0)}",
        f"- 标题正文inconsistent数量：{consistency_counts.get('inconsistent', 0)}",
        f"- 标题正文uncertain数量：{consistency_counts.get('uncertain', 0)}",
        f"- 标题正文not_applicable数量：{consistency_counts.get('not_applicable', 0)}",
        f"- 父文档类型变化：{len(parent_changes)}",
        f"- 子章节类型/标题/类别变化：{len(segment_changes)}", "",
        "## 验收状态", "",
        "- 自动分类、标题恢复和一致性状态均为pending，需审核包人工确认。",
        "- 本轮未执行全量清洗、爬取或知识库导入。",
    ]
    (REPORTS_V241 / "pilot_100_report_v2_4_1.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    pytest_path = REPORTS_V241 / "pytest_v2_4_1.txt"
    pytest_text = pytest_path.read_text(encoding="utf-8") if pytest_path.exists() else "测试输出待刷新"
    (REPORTS_V241 / "regression_test_report_v2_4_1.md").write_text("# V2.4.1回归测试报告\n\n```text\n" + pytest_text.strip() + "\n```\n", encoding="utf-8")


if __name__ == "__main__":
    main()
