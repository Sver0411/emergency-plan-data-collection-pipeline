"""V2.4.2.1 fixed-100 hotfix runner."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .classification_v2421 import (
    classify_segment_v2421,
    recover_hotfix_title,
    select_segment_classification_text,
)
from .config import PILOT_SEED
from .pilot_runner_v2_4 import dump
from .section_splitter_v24 import split_sections_v24
from .title_audit_v242 import audit_title_consistency_v242


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "cleaned_v2_4_2" / "pilot_100_results_v2_4_2.json"
STABLE = ROOT / "cleaned_v2_4_1" / "pilot_100_results_v2_4_1.json"
BASE_PARENTS = ROOT / "cleaned_v2_4_2" / "parent_documents_v2_4_2.json"
CLEANED = ROOT / "cleaned_v2_4_2_1"
REPORTS = ROOT / "reports_v2_4_2_1"


def _pending(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "review_status":
                value[key] = "pending"
            else:
                _pending(item)
    elif isinstance(value, list):
        for item in value:
            _pending(item)


def _review(path: Path, heading: str, records: list[dict]) -> None:
    lines = [f"# {heading}", "", "所有记录保持 pending，以下内容等待人工审核。", ""]
    for index, item in enumerate(records, 1):
        audit = item.get("segment_classification_audit", {})
        lines.extend([
            f"## {index}. {item['document_id']}", "",
            f"- 父文档：{item.get('parent_document_id', '')}",
            f"- 父类型：{item.get('parent_document_type', '')}",
            f"- 子章节类型：{item.get('segment_type', '')}",
            f"- 标题：{item.get('title', '')}",
            f"- 标题状态：{item.get('title_status', '')}",
            f"- 事故类别：{item.get('accident_category', '')}",
            f"- 分类文本来源：{item.get('segment_classification_text_source', '')}",
            f"- 局部文本长度：{item.get('segment_local_text_length', 0)}",
            f"- 是否使用父上下文：{item.get('parent_context_used', False)}",
            f"- 结构证据：{audit.get('structure_evidence', [])}",
            f"- 目录行/非空行：{audit.get('toc_line_count', 0)}/{audit.get('nonempty_line_count', 0)}",
            f"- 连续正文：{audit.get('continuous_body', False)}",
            f"- 编号处置动作：{audit.get('numbered_action_count', 0)}",
            f"- 警告：{item.get('warnings', [])}",
            "- 人工子章节类型：________",
            "- 人工标题：________",
            "- 人工备注：________", "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    CLEANED.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    base_rows = json.loads(BASE.read_text(encoding="utf-8"))
    stable_rows = json.loads(STABLE.read_text(encoding="utf-8"))
    stable_by_id = {item["document_id"]: item for item in stable_rows}
    parent_rows = json.loads(BASE_PARENTS.read_text(encoding="utf-8"))

    output: list[dict] = []
    changes: list[dict] = []
    review_queue: list[dict] = []
    for base in base_rows:
        record = dict(base)
        local_text, source, parent_used = select_segment_classification_text(record)
        previous_stable = stable_by_id.get(record["document_id"], {}).get("segment_type", "")
        new_type, audit = classify_segment_v2421(record, previous_stable)
        title_decision = recover_hotfix_title(record, new_type, local_text)
        old_type = record.get("segment_type", "")
        old_title = record.get("title", "")
        old_status = record.get("title_status", "")
        changed = old_type != new_type or old_title != title_decision["title"] or old_status != title_decision["status"]

        category = record.get("accident_category", "other")
        title_probe = str(title_decision["title"])
        if new_type == "onsite_disposal_plan" and re.search(r"火灾|易燃液体|压缩或液化气体", title_probe):
            category = "fire_explosion"
        if new_type == "full_document" and record.get("parent_document_type") == "organization_plan" and re.search(r"有限空间|受限空间", local_text[:2500]):
            category = "confined_space"

        sections = record.get("sections", [])
        if changed and new_type not in {"toc_fragment", "list_fragment", "reference_sentence"}:
            sections = [section.model_dump() for section in split_sections_v24(local_text, category)]
        consistency = audit_title_consistency_v242(
            document_id=record["document_id"],
            title=str(title_decision["title"]),
            text=local_text,
            segment_type=new_type,
            title_status=str(title_decision["status"]),
            title_source=str(title_decision["source"]),
            sections=sections,
        )
        warnings = [warning for warning in record.get("warnings", []) if warning not in {
            "toc_only", "title_body_inconsistent", "title_body_uncertain",
            "body_title_concatenation_fixed", "residual_fragment", "boundary_error",
        }]
        if changed:
            warnings.append("segment_type_hotfix_review")
        if consistency["status"] == "inconsistent":
            warnings.append("title_body_inconsistent")
        elif consistency["status"] == "uncertain":
            warnings.append("title_body_uncertain")
        warnings = list(dict.fromkeys(warnings))

        role = record.get("content_role", "")
        if new_type == "full_document":
            role = record.get("classification_reasons") and record.get("content_role") or "plan_structure_reference"
            if role in {"toc_reference", "list_reference"}:
                role = "plan_structure_reference"
        elif new_type == "onsite_disposal_plan":
            role = "disposal_knowledge"
        elif new_type == "toc_fragment":
            role = "toc_reference"
        elif new_type == "list_fragment":
            role = "list_reference"

        record.update({
            "segment_type": new_type,
            "title": title_decision["title"],
            "title_status": title_decision["status"],
            "title_source": title_decision["source"],
            "title_recovery_evidence": title_decision["evidence"],
            "accident_category": category,
            "content_role": role,
            "sections": sections,
            "title_consistency": consistency,
            "warnings": warnings,
            "segment_classification_text_source": source,
            "segment_local_text_length": len(local_text),
            "parent_context_used": parent_used,
            "segment_classification_audit": audit,
            "requires_manual_review": bool(record.get("requires_manual_review") or changed or title_decision["status"] != "valid" or consistency["status"] in {"inconsistent", "uncertain"}),
            "quality_grade": "manual_review" if changed or title_decision["status"] != "valid" else record.get("quality_grade", "silver_candidate"),
            "review_status": "pending",
        })
        _pending(record)
        output.append(record)
        if changed:
            changes.append({
                "document_id": record["document_id"],
                "parent_document_id": record["parent_document_id"],
                "before": {"segment_type": old_type, "title": old_title, "title_status": old_status},
                "after": {"segment_type": new_type, "title": record["title"], "title_status": record["title_status"]},
                "segment_classification_text_source": source,
                "segment_local_text_length": len(local_text),
                "parent_context_used": parent_used,
                "review_status": "pending",
            })
        if record["requires_manual_review"]:
            review_queue.append({
                "document_id": record["document_id"],
                "parent_document_id": record["parent_document_id"],
                "stage": "v2_4_2_1_segment_hotfix_review",
                "reason": warnings + consistency["reasons"],
                "review_status": "pending",
            })

    # Parent records are copied unchanged; V2.4.2.1 has no parent classifier.
    for item in parent_rows:
        item["review_status"] = "pending"
    dump(CLEANED / "pilot_100_results_v2_4_2_1.json", output)
    dump(CLEANED / "parent_documents_v2_4_2_1.json", parent_rows)
    dump(CLEANED / "segment_records_v2_4_2_1.json", output)
    dump(CLEANED / "manual_review_queue_v2_4_2_1.json", review_queue)

    packages = {
        "toc_fragments": [item for item in output if item["segment_type"] == "toc_fragment"],
        "full_documents": [item for item in output if item["segment_type"] == "full_document"],
        "onsite_disposal_plans": [item for item in output if item["segment_type"] == "onsite_disposal_plan"],
        "list_fragments": [item for item in output if item["segment_type"] == "list_fragment"],
    }
    for name, records in packages.items():
        dump(REPORTS / f"manual_review_{name}_v2_4_2_1.json", records)
        _review(REPORTS / f"manual_review_{name}_v2_4_2_1.md", f"V2.4.2.1 {name}审核包", records)

    dump(REPORTS / "segment_type_changes_v2_4_2_1.json", changes)
    _review(
        REPORTS / "segment_type_changes_v2_4_2_1.md",
        "V2.4.2.1子章节类型变化报告",
        [item for item in output if any(change["document_id"] == item["document_id"] for change in changes)],
    )
    regression_records = [
        item for item in output
        if any(change["document_id"] == item["document_id"] for change in changes)
        and base_rows[[row["document_id"] for row in base_rows].index(item["document_id"])]["segment_type"] in {"toc_fragment", "list_fragment"}
    ]
    dump(REPORTS / "hotfix_regression_records_v2_4_2_1.json", regression_records)
    _review(REPORTS / "hotfix_regression_records_v2_4_2_1.md", "V2.4.2.1目录/列表回归记录专项报告", regression_records)

    segment_counts = Counter(item["segment_type"] for item in output)
    source_counts = Counter(item["segment_classification_text_source"] for item in output)
    base_parent_types = {item["parent_document_id"]: item["parent_document_type"] for item in parent_rows}
    current_parent_types = Counter(item["parent_document_type"] for item in output)
    parent_changes = sum(
        item.get("parent_document_type") != base_parent_types.get(item["parent_document_id"])
        for item in output
    )
    report = [
        "# V2.4.2.1固定100条试运行报告", "",
        f"- 固定种子：{PILOT_SEED}",
        "- 是否重新抽样：否",
        "- 是否重新解析：否",
        f"- 输入锁定记录：{len([item for item in output if not item['document_id'].endswith('-appendix-sds')])}",
        f"- 结构化记录：{len(output)}",
        f"- 独立父文档：{len(base_parent_types)}",
        f"- 父文档类型变化：{parent_changes}", "",
        "## 分类文本来源", "",
    ]
    report.extend(f"- {key}：{value}" for key, value in sorted(source_counts.items()))
    report.extend(["", "## 子章节数量", ""])
    for key in ("full_document", "embedded_special_section", "onsite_disposal_plan", "guidance_section", "accident_case_section", "reference_sentence", "list_fragment", "toc_fragment", "table", "appendix_sds", "embedded_sds"):
        report.append(f"- {key}：{segment_counts.get(key, 0)}")
    report.extend([
        "", "## 热修复统计", "",
        f"- 子章节/标题变化记录：{len(changes)}",
        f"- 人工审核队列：{len(review_queue)}",
        f"- 所有结果保持pending：{all(item.get('review_status') == 'pending' for item in output)}",
        f"- parent_context_used=true：{sum(bool(item.get('parent_context_used')) for item in output)}",
        "", "## 限制", "",
        "- 本轮未修改父文档分类、原始资料或业务系统。",
        "- 本轮未重新爬取、未执行全量清洗、未导入知识库。",
    ])
    (REPORTS / "pilot_100_report_v2_4_2_1.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    pytest_path = REPORTS / "pytest_v2_4_2_1.txt"
    pytest_text = pytest_path.read_text(encoding="utf-8") if pytest_path.exists() else "pytest输出待写入"
    (REPORTS / "regression_test_report_v2_4_2_1.md").write_text(
        "# V2.4.2.1回归测试报告\n\n" + pytest_text.strip() + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
