"""V2.4.2.2 fixed-sample title-only hotfix runner."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import PILOT_SEED
from .pilot_runner_v2_4 import dump
from .title_hotfix_v2422 import (
    audit_full_document_title_v2422,
    extract_sds_identity,
    recover_full_document_title_v2422,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "cleaned_v2_4_2_1" / "pilot_100_results_v2_4_2_1.json"
CLEANED = ROOT / "cleaned_v2_4_2_2"
REPORTS = ROOT / "reports_v2_4_2_2"

FROZEN_FIELDS = (
    "parent_document_type",
    "segment_type",
    "accident_category",
    "content_role",
    "parent_type_locked",
    "parent_type_evidence_scope",
    "embedded_content_types",
)
TITLE_WARNING_NAMES = {
    "toc_only",
    "title_body_inconsistent",
    "title_body_uncertain",
    "first_aid_text_as_title",
    "invalid_title_candidate",
    "title_recovery_uncertain",
    "title_missing",
    "sentence_used_as_title",
}


def _sample_signature(rows: list[dict[str, Any]]) -> str:
    payload = "\n".join(sorted(str(row.get("document_id") or "") for row in rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _title_review_markdown(heading: str, audits: list[dict[str, Any]]) -> str:
    lines = [f"# {heading}", "", "固定样本标题审核；所有记录均保持 `review_status = pending`。", ""]
    for index, item in enumerate(audits, 1):
        lines.extend([
            f"## {index}. {item['document_id']}", "",
            f"- `document_id`：{item['document_id']}",
            f"- `parent_document_id`：{item.get('parent_document_id', '')}",
            f"- `parent_document_type`：{item.get('parent_document_type', '')}",
            f"- `segment_type`：{item.get('segment_type', '')}",
            f"- 原标题：{item.get('before_title', '')}",
            f"- 修复后标题：{item.get('after_title', '')}",
            f"- `title_status`：{item.get('title_status', '')}",
            f"- `title_source`：{item.get('title_source', '')}",
            f"- 修复原因：{item.get('reasons', [])}",
            f"- 硬性校验问题：{item.get('hard_issues', [])}",
            f"- 标题正文一致性：{item.get('consistency_status', '')}",
            f"- 是否进入人工审核：{item.get('requires_manual_review', False)}",
            f"- 来源URL：{item.get('source_url', '')}",
            "- 人工确认标题：________",
            "- 人工审核备注：________",
            "",
        ])
    return "\n".join(lines) + "\n"


def _change_markdown(changes: list[dict[str, Any]]) -> str:
    lines = [
        "# V2.4.2.2 full_document 标题修复前后变化",
        "",
        f"共 {len(changes)} 条 full_document 的标题相关字段发生变化；分类字段未参与重算。",
        "",
    ]
    for index, item in enumerate(changes, 1):
        lines.extend([
            f"## {index}. {item['document_id']}", "",
            f"- 父类型/子类型：{item['parent_document_type']} / {item['segment_type']}",
            f"- 原标题：{item['before']['title']}",
            f"- 新标题：{item['after']['title']}",
            f"- 状态：{item['before']['title_status']} → {item['after']['title_status']}",
            f"- 来源：{item['before']['title_source']} → {item['after']['title_source']}",
            f"- 一致性：{item['before']['title_consistency']} → {item['after']['title_consistency']}",
            f"- 原因：{item['reasons']}",
            "",
        ])
    return "\n".join(lines) + "\n"


def main(pytest_result: str = "未提供") -> None:
    CLEANED.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)

    base_rows: list[dict[str, Any]] = json.loads(BASE.read_text(encoding="utf-8"))
    output: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for base in base_rows:
        record = copy.deepcopy(base)
        if record.get("segment_type") == "full_document":
            decision = recover_full_document_title_v2422(record)
            title_audit = audit_full_document_title_v2422(record, decision)
            status = title_audit["status"]
            consistency = title_audit["consistency"]
            issues = title_audit["issues"]

            old_state = {
                "title": record.get("title", ""),
                "title_status": record.get("title_status", ""),
                "title_source": record.get("title_source", ""),
                "title_consistency": (record.get("title_consistency") or {}).get("status", ""),
            }
            record["title"] = decision["title"]
            record["title_status"] = status
            record["title_source"] = decision["source"]
            record["title_recovery_evidence"] = decision["evidence"]
            record["title_consistency"] = consistency
            record["requires_manual_review"] = bool(
                record.get("requires_manual_review")
                or status != "valid"
                or consistency.get("status") in {"inconsistent", "uncertain"}
            )

            warnings = [warning for warning in record.get("warnings", []) if warning not in TITLE_WARNING_NAMES]
            if status in {"invalid", "missing"}:
                warnings.append("full_document_title_invalid_or_missing")
            elif status == "uncertain":
                warnings.append("full_document_title_uncertain")
            if consistency.get("status") == "inconsistent":
                warnings.append("title_body_inconsistent")
            elif consistency.get("status") == "uncertain":
                warnings.append("title_body_uncertain")
            record["warnings"] = list(dict.fromkeys(warnings))
            record["title_hotfix_v2_4_2_2"] = {
                "execution_order": [
                    "frozen_parent_document_type",
                    "frozen_final_segment_type",
                    "recover_title_after_final_segment_type",
                    "hard_title_validation",
                    "title_body_consistency",
                    "review_routing",
                ],
                "before_title": old_state["title"],
                "after_title": record["title"],
                "reasons": decision["reasons"],
                "hard_issues": issues,
                "review_status": "pending",
            }
            new_state = {
                "title": record["title"],
                "title_status": record["title_status"],
                "title_source": record["title_source"],
                "title_consistency": consistency.get("status", ""),
            }
            audit_record = {
                "document_id": record.get("document_id", ""),
                "parent_document_id": record.get("parent_document_id", ""),
                "parent_document_type": record.get("parent_document_type", ""),
                "segment_type": record.get("segment_type", ""),
                "before_title": old_state["title"],
                "after_title": record["title"],
                "title_status": record["title_status"],
                "title_source": record["title_source"],
                "reasons": decision["reasons"],
                "hard_issues": issues,
                "consistency_status": consistency.get("status", ""),
                "requires_manual_review": record["requires_manual_review"],
                "source_url": record.get("source_url", ""),
            }
            audits.append(audit_record)
            if old_state != new_state:
                changes.append({
                    "document_id": record.get("document_id", ""),
                    "parent_document_type": record.get("parent_document_type", ""),
                    "segment_type": record.get("segment_type", ""),
                    "before": old_state,
                    "after": new_state,
                    "reasons": decision["reasons"],
                })

        # This task never upgrades approval state.  The assertion prevents a
        # silent mutation if a future base file contains a non-pending row.
        if record.get("review_status") != "pending":
            raise RuntimeError(f"non-pending base row: {record.get('document_id')}")
        output.append(record)

    frozen_changes: list[dict[str, Any]] = []
    for before, after in zip(base_rows, output, strict=True):
        for field in FROZEN_FIELDS:
            if before.get(field) != after.get(field):
                frozen_changes.append({
                    "document_id": before.get("document_id", ""),
                    "field": field,
                    "before": before.get(field),
                    "after": after.get(field),
                })
    if frozen_changes:
        raise RuntimeError(f"frozen classification fields changed: {frozen_changes[:3]}")
    if _sample_signature(base_rows) != _sample_signature(output):
        raise RuntimeError("fixed sample IDs changed")

    full_rows = [row for row in output if row.get("segment_type") == "full_document"]
    sds_rows = [row for row in full_rows if row.get("parent_document_type") == "sds"]
    status_counts = Counter(str(row.get("title_status") or "") for row in full_rows)
    consistency_counts = Counter(str((row.get("title_consistency") or {}).get("status") or "") for row in full_rows)
    valid_audits = [item for item in audits if item["title_status"] == "valid"]
    forbidden_valid = Counter(issue for item in valid_audits for issue in item["hard_issues"])
    full_toc_only = sum(row.get("title_status") == "toc_only" for row in full_rows)
    all_pending = all(row.get("review_status") == "pending" for row in output)
    traceable = sum(bool(row.get("source_url") and row.get("source_file")) for row in output)

    output_path = CLEANED / "pilot_100_results_v2_4_2_2.json"
    dump(output_path, output)
    (REPORTS / "full_document_title_audit_v2_4_2_2.md").write_text(
        _title_review_markdown("V2.4.2.2 全部 full_document 标题审核包", audits), encoding="utf-8"
    )
    sds_ids = {row.get("document_id") for row in sds_rows}
    sds_audits = [item for item in audits if item["document_id"] in sds_ids]
    sds_text = _title_review_markdown("V2.4.2.2 全部 SDS 标题审核包", sds_audits)
    sds_text += "\n## SDS身份提取复核\n\n"
    for row in sds_rows:
        identity, identity_source = extract_sds_identity(row)
        sds_text += f"- `{row['document_id']}`：身份=`{identity}`；证据来源=`{identity_source}`；标题=`{row['title']}`\n"
    (REPORTS / "sds_title_audit_v2_4_2_2.md").write_text(sds_text, encoding="utf-8")
    (REPORTS / "title_changes_v2_4_2_2.md").write_text(_change_markdown(changes), encoding="utf-8")

    regression_names = [
        "full_document热修复后重新运行标题恢复",
        "full_document优先使用父文档规范标题",
        "SDS急救句不能作为标题",
        "SDS供应商信息不能作为标题",
        "SDS章节名不能作为整份SDS标题",
        "事故通报正文句不能作为标题",
        "法律依据条目不能作为预案标题",
        "full_document不得使用toc_only",
        "正文二级章节不能覆盖父文档标题",
        "父文档和子章节分类字段保持冻结",
        "所有结果保持pending",
    ]
    regression_lines = [
        "# V2.4.2.2 回归测试报告", "",
        f"- pytest结果：`{pytest_result}`",
        f"- 新增回归测试：{len(regression_names)} 项", "",
        "## 新增测试", "",
    ] + [f"{index}. {name}" for index, name in enumerate(regression_names, 1)]
    regression_lines.extend([
        "", "## 冻结校验", "",
        f"- 父文档类型变化：{sum(item['field'] == 'parent_document_type' for item in frozen_changes)}",
        f"- 子章节类型变化：{sum(item['field'] == 'segment_type' for item in frozen_changes)}",
        f"- accident_category变化：{sum(item['field'] == 'accident_category' for item in frozen_changes)}",
        f"- content_role变化：{sum(item['field'] == 'content_role' for item in frozen_changes)}",
        f"- 全部pending：{all_pending}",
    ])
    (REPORTS / "regression_test_report_v2_4_2_2.md").write_text("\n".join(regression_lines) + "\n", encoding="utf-8")

    acceptance = {
        "full_document_toc_only_zero": full_toc_only == 0,
        "valid_first_aid_zero": forbidden_valid.get("first_aid_sentence", 0) == 0,
        "valid_supplier_information_zero": forbidden_valid.get("supplier_information", 0) == 0,
        "valid_list_or_legal_reference_zero": forbidden_valid.get("list_or_legal_reference", 0) == 0,
        "valid_body_sentence_or_fragment_zero": forbidden_valid.get("body_sentence_or_fragment", 0) == 0,
        "frozen_classification_change_zero": not frozen_changes,
        "all_pending": all_pending,
        "same_fixed_sample": _sample_signature(base_rows) == _sample_signature(output),
        "pytest_passed": "passed" in pytest_result and "failed" not in pytest_result,
    }
    report_lines = [
        "# V2.4.2.2 最终标题热修复报告", "",
        "本轮在 V2.4.2.1 固定结果上仅重算 `full_document` 标题相关字段；未重新解析、爬取、抽样、全量清洗或导入数据库。",
        "",
        "## 输入与冻结范围", "",
        f"- 固定种子：{PILOT_SEED}",
        f"- 输入结构化记录：{len(base_rows)}",
        f"- 输出结构化记录：{len(output)}",
        f"- 固定样本签名：`{_sample_signature(output)}`",
        "- 重新抽样：否",
        "- 重新解析：否",
        "- 父文档分类重算：否",
        "- 子章节分类重算：否",
        f"- 父文档类型变化：{sum(item['field'] == 'parent_document_type' for item in frozen_changes)}",
        f"- 子章节类型变化：{sum(item['field'] == 'segment_type' for item in frozen_changes)}",
        f"- accident_category变化：{sum(item['field'] == 'accident_category' for item in frozen_changes)}",
        f"- content_role变化：{sum(item['field'] == 'content_role' for item in frozen_changes)}",
        "",
        "## full_document标题结果", "",
        f"- full_document数量：{len(full_rows)}",
        f"- SDS full_document数量：{len(sds_rows)}",
        f"- 标题变化数量：{len(changes)}",
        f"- title_status：{dict(status_counts)}",
        f"- title_consistency：{dict(consistency_counts)}",
        f"- full_document中的toc_only：{full_toc_only}",
        f"- valid标题硬性问题：{dict(forbidden_valid)}",
        "",
        "## 审核与追溯", "",
        f"- 人工审核full_document：{sum(item['requires_manual_review'] for item in audits)}",
        f"- 来源文件与URL同时可追溯：{traceable}/{len(output)}",
        f"- 所有记录保持pending：{all_pending}",
        "",
        "## pytest", "",
        f"- `{pytest_result}`",
        "",
        "## 验收条件", "",
    ]
    report_lines.extend(f"- {key}：{value}" for key, value in acceptance.items())
    report_lines.extend([
        "", f"- V2.4.2.2系统性验收条件：{all(acceptance.values())}",
        "- 执行全量清洗：否",
        "- 导入数据库/知识库：否",
    ])
    (REPORTS / "pilot_100_title_hotfix_report_v2_4_2_2.md").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-result", default="未提供")
    args = parser.parse_args()
    main(pytest_result=args.pytest_result)
