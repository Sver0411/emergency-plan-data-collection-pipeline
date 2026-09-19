"""Apply the final title-only preflight patch to explicitly selected rows.

The caller supplies document IDs on the command line.  No regression ID is
embedded in production logic, and no parent/segment/category/role classifier
is invoked.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from .pilot_runner_v2_4 import dump
from .title_hotfix_v2422 import audit_full_document_title_v2422, recover_full_document_title_v2422


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "cleaned_v2_4_2_2" / "pilot_100_results_v2_4_2_2.json"
REPORT = ROOT / "reports_v2_4_2_2" / "preflight_patch_report.md"
FROZEN_FIELDS = ("parent_document_type", "segment_type", "accident_category", "content_role")


def apply_selected(document_ids: set[str], *, report_only: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = json.loads(RESULTS.read_text(encoding="utf-8"))
    found: set[str] = set()
    audits: list[dict[str, Any]] = []

    for record in rows:
        if record.get("document_id") not in document_ids:
            continue
        found.add(str(record["document_id"]))
        before_frozen = {field: copy.deepcopy(record.get(field)) for field in FROZEN_FIELDS}
        before_title = str(record.get("title") or "")
        before_status = str(record.get("title_status") or "")

        if not report_only and before_status == "missing":
            decision = recover_full_document_title_v2422(record)
            title_audit = audit_full_document_title_v2422(record, decision)
            if decision.get("reasons") != ["preflight_first_line_special_plan_title"]:
                raise RuntimeError(f"selected row lacks first-line special-plan evidence: {record['document_id']}")
            if title_audit["status"] != "valid" or title_audit["issues"]:
                raise RuntimeError(f"selected row failed title hard checks: {record['document_id']}")
            record["title"] = decision["title"]
            record["title_status"] = "valid"
            record["title_source"] = "raw_text_document_title"
            record["title_recovery_evidence"] = decision["evidence"]
            record["title_consistency"] = title_audit["consistency"]
            warnings = [
                warning for warning in record.get("warnings", [])
                if warning not in {
                    "full_document_title_invalid_or_missing", "title_body_uncertain",
                    "title_body_inconsistent", "full_document_title_uncertain",
                }
            ]
            warnings.append("preflight_first_line_title_recovered")
            record["warnings"] = list(dict.fromkeys(warnings))
            record["preflight_title_patch"] = {
                "before_title": before_title,
                "after_title": record["title"],
                "rule": "first_valid_line_special_plan_with_topic_and_structure_evidence",
                "classification_fields_before": before_frozen,
                "classification_fields_after": {field: copy.deepcopy(record.get(field)) for field in FROZEN_FIELDS},
                "review_status": "pending",
            }

        after_frozen = {field: record.get(field) for field in FROZEN_FIELDS}
        if before_frozen != after_frozen:
            raise RuntimeError(f"frozen classification field changed: {record['document_id']}")
        if record.get("review_status") != "pending":
            raise RuntimeError(f"review status changed: {record['document_id']}")
        audits.append({
            "document_id": record["document_id"],
            "before_title": before_title,
            "after_title": record.get("title", ""),
            "title_status": record.get("title_status", ""),
            "title_source": record.get("title_source", ""),
            "classification_changes": sum(before_frozen[field] != after_frozen[field] for field in FROZEN_FIELDS),
            "review_status": record.get("review_status", ""),
        })

    missing_ids = document_ids - found
    if missing_ids:
        raise RuntimeError(f"selected IDs not found: {sorted(missing_ids)}")
    if not report_only:
        dump(RESULTS, rows)
    return rows, audits


def write_report(audits: list[dict[str, Any]], pytest_result: str) -> None:
    restored = all(
        item["title_status"] == "valid"
        and item["title_source"] == "raw_text_document_title"
        and bool(item["after_title"])
        for item in audits
    )
    classification_changes = sum(item["classification_changes"] for item in audits)
    pytest_passed = "passed" in pytest_result and "failed" not in pytest_result
    all_pending = all(item["review_status"] == "pending" for item in audits)
    ready = restored and classification_changes == 0 and pytest_passed and all_pending
    lines = [
        "# V2.4.2.2 全量清洗前最后预检补丁", "",
        "本补丁仅检查并恢复两条 `full_document` 首行专项预案标题，未重新运行固定100条。", "",
        "## 定点结果", "",
    ]
    for item in audits:
        lines.extend([
            f"- `{item['document_id']}`：`{item['after_title']}`；"
            f"status=`{item['title_status']}`；source=`{item['title_source']}`；pending={item['review_status'] == 'pending'}",
        ])
    lines.extend([
        "", "## 验收", "",
        f"- 两条标题均恢复：{restored}",
        f"- 父文档类型、segment_type、accident_category、content_role变化：{classification_changes}",
        f"- pytest：`{pytest_result}`",
        f"- 所有结果保持pending：{all_pending}",
        f"- 具备第一次全量清洗条件：{ready}",
        "- 执行全量清洗：否",
        "- 导入数据库：否",
    ])
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-id", action="append", required=True)
    parser.add_argument("--pytest-result", default="待运行")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    _, audits = apply_selected(set(args.document_id), report_only=args.report_only)
    write_report(audits, args.pytest_result)


if __name__ == "__main__":
    main()
