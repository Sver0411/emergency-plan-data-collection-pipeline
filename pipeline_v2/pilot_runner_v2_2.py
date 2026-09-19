from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from .classification import category_of, classify, content_role_of, segment_type_of
from .config import PILOT_SEED, PILOT_SIZE, REPORTS, SOURCE_RECORDS
from .knowledge_extractor import extract_knowledge
from .normalization import CAS_RE, PHONE_RE, normalize_text
from .sds_matcher import sds_from_text
from .section_splitter import is_toc_fragment, split_sections

ROOT = SOURCE_RECORDS.parent.parent
CLEANED_V22 = ROOT / "cleaned_v2_2"
REPORTS_V22 = ROOT / "reports_v2_2"
LOCKED_MANIFEST = REPORTS / "pilot_sample_manifest_locked_v2_1.json"
BEFORE_RESULTS = ROOT / "cleaned_v2" / "pilot_100_results.json"


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def record_id(index: int, row: dict) -> str:
    seed = f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def parent_id(row: dict) -> str:
    identity = row.get("file") or row.get("url") or row.get("title") or "missing-parent"
    return "file-" + hashlib.sha256(str(identity).encode()).hexdigest()[:16]


def locked_records(rows: list[dict]) -> list[tuple[int, dict, str]]:
    manifest = json.loads(LOCKED_MANIFEST.read_text(encoding="utf-8"))
    index = {record_id(i, row): (i, row) for i, row in enumerate(rows)}
    selected = []
    for item in manifest["records"]:
        ident = item["document_id"]
        if ident not in index:
            raise RuntimeError(f"locked record missing: {ident}")
        i, row = index[ident]
        selected.append((i, row, item.get("sampling_bucket", "locked")))
    if len(selected) != PILOT_SIZE:
        raise RuntimeError(f"locked sample changed: expected {PILOT_SIZE}, got {len(selected)}")
    return selected


def _parent_context(items: list[tuple[int, dict, str]]) -> tuple[str, str, str]:
    titles, contents, types = [], [], []
    seen_content = set()
    for _, row, _ in items:
        title = (row.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
        content = normalize_text(row.get("content") or "")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if content and digest not in seen_content:
            contents.append(content)
            seen_content.add(digest)
        if row.get("type"):
            types.append(row["type"])
    return "｜".join(titles)[:4000], "\n\n".join(contents)[:120000], Counter(types).most_common(1)[0][0] if types else ""


def _review_parent_30(records: list[dict]) -> list[dict]:
    by_parent = defaultdict(list)
    for record in records:
        by_parent[record["parent_document_id"]].append(record)
    representatives = []
    for parent, values in by_parent.items():
        representative = max(values, key=lambda item: len(item["normalized_text"]))
        representatives.append({
            "parent_document_id": parent,
            "representative_document_id": representative["document_id"],
            "file_name": Path(representative["source_file"]).name,
            "source_url": representative["source_url"],
            "parent_document_type": representative["parent_document_type"],
            "classification_reasons": representative["classification_reasons"],
            "classification_evidence": representative["classification_evidence"],
            "classification_confidence": representative["classification_confidence"],
            "segment_count_in_pilot": len(values),
            "human_parent_type": "",
            "human_notes": "",
            "review_status": "pending",
        })
    rng = random.Random(PILOT_SEED + 2201)
    by_type = defaultdict(list)
    for item in representatives:
        by_type[item["parent_document_type"]].append(item)
    selected = []
    # First cover every available parent type, then fill proportionally without repeating a parent.
    for kind in sorted(by_type):
        rng.shuffle(by_type[kind])
        selected.append(by_type[kind].pop())
    remaining = [item for values in by_type.values() for item in values]
    rng.shuffle(remaining)
    selected.extend(remaining[:max(0, 30 - len(selected))])
    return selected[:30]


def _review_segment_30(records: list[dict]) -> list[dict]:
    rng = random.Random(PILOT_SEED + 2202)
    candidates = records[:]
    rng.shuffle(candidates)
    candidates.sort(key=lambda item: (item["segment_type"] == "full_document", not item["sections"], item["parent_document_id"]))
    parent_counts, selected = Counter(), []
    for record in candidates:
        if parent_counts[record["parent_document_id"]] >= 2:
            continue
        selected.append({
            "document_id": record["document_id"], "parent_document_id": record["parent_document_id"],
            "file_name": Path(record["source_file"]).name, "source_url": record["source_url"],
            "parent_document_type": record["parent_document_type"], "segment_type": record["segment_type"],
            "accident_category": record["accident_category"], "content_role": record["content_role"],
            "title": record["title"],
            "section_boundaries": [{"heading": s["heading"], "start": s["start_position"], "end": s["end_position"]} for s in record["sections"][:12]],
            "warnings": record["warnings"],
            "human_segment_type": "", "human_boundary_correct": None,
            "human_false_headings": [], "human_notes": "", "review_status": "pending",
        })
        parent_counts[record["parent_document_id"]] += 1
        if len(selected) == 30:
            break
    return selected


def _write_review_markdown(path: Path, title: str, items: list[dict], parent_mode: bool) -> None:
    lines = [f"# {title}", "", "所有记录均为 `pending`。请人工填写空白字段，程序不自行计算人工准确率。", ""]
    for index, item in enumerate(items, start=1):
        lines += [f"## {index}. {item.get('parent_document_id') if parent_mode else item.get('document_id')}", ""]
        if parent_mode:
            lines += [
                f"- 文件：{item['file_name']}", f"- 来源：{item['source_url'] or '缺失，需核查'}",
                f"- V2.2父文档类型：{item['parent_document_type']}", f"- 本批子记录数：{item['segment_count_in_pilot']}",
                f"- 置信度：{item['classification_confidence']}",
                f"- 证据：{json.dumps(item['classification_evidence'], ensure_ascii=False)}",
                "- 人工父文档类型：________", "- 人工备注：________", "",
            ]
        else:
            lines += [
                f"- 父文档：{item['parent_document_id']}", f"- 文件：{item['file_name']}",
                f"- 父类型：{item['parent_document_type']}", f"- 子章节类型：{item['segment_type']}",
                f"- 事故类别：{item['accident_category']}", f"- 标题：{item['title']}",
                f"- 边界：{json.dumps(item['section_boundaries'], ensure_ascii=False)}",
                "- 人工子章节类型：________", "- 边界是否正确：________", "- 假标题：________", "- 人工备注：________", "",
            ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    CLEANED_V22.mkdir(exist_ok=True)
    REPORTS_V22.mkdir(exist_ok=True)
    rows = json.loads(SOURCE_RECORDS.read_text(encoding="utf-8"))
    selected = locked_records(rows)
    groups = defaultdict(list)
    for item in selected:
        groups[parent_id(item[1])].append(item)

    parent_classifications = {}
    for parent, items in groups.items():
        title, text, original_type = _parent_context(items)
        representative = max(items, key=lambda item: len(item[1].get("content") or ""))[1]
        parent_classifications[parent] = classify(
            parent, title, text, representative.get("url") or "", original_type,
            parent_text=text, sibling_count=len(items),
        )

    output, manual_queue = [], []
    for index, row, sampling_bucket in selected:
        ident, parent = record_id(index, row), parent_id(row)
        normalized = normalize_text(row.get("content") or "")
        parent_result = parent_classifications[parent]
        segment_type = segment_type_of(row.get("title") or "", normalized, parent_result.parent_document_type, len(groups[parent]))
        # Text statistics alone can confuse short guidance tables with a contents page.
        # Require title-level navigation evidence before overriding the segment role.
        if is_toc_fragment(normalized) and (re.fullmatch(r"(?:目录|目\s*录)(?:片段)?", (row.get("title") or "").strip()) or re.search(r"\.{5,}|…{3,}", row.get("title") or "")):
            segment_type = "toc_fragment"
        category = category_of(row.get("title") or "", normalized)
        role = content_role_of(parent_result.parent_document_type, segment_type)
        sections = [] if segment_type == "toc_fragment" else split_sections(normalized, category)
        knowledge = [item.model_dump() for item in extract_knowledge(ident, row.get("url") or "", sections)]
        sds = sds_from_text(ident, normalized, row.get("url") or "") if parent_result.parent_document_type == "sds" else None
        warnings = []
        if parent_result.requires_manual_review:
            warnings.append("parent_classification_requires_manual_review")
        if segment_type == "toc_fragment":
            warnings.append("toc_navigation_only_not_template_candidate")
        record = {
            "document_id": ident, "parent_document_id": parent,
            "source_file": row.get("file") or "[missing]", "source_url": row.get("url") or "",
            "title": row.get("title") or "", "raw_text": row.get("content") or "", "normalized_text": normalized,
            "parent_document_type": parent_result.parent_document_type, "segment_type": segment_type,
            "accident_category": category, "content_role": role,
            "classification_reasons": parent_result.classification_reasons,
            "classification_evidence": parent_result.classification_evidence,
            "classification_confidence": parent_result.classification_confidence,
            "requires_manual_review": parent_result.requires_manual_review,
            "sections": [section.model_dump() for section in sections], "knowledge_items": knowledge,
            "sds": sds.model_dump() if sds else None, "warnings": warnings,
            "sampling_bucket": sampling_bucket,
            "quality_grade": "manual_review" if parent_result.requires_manual_review else "silver_candidate",
            "review_status": "pending",
        }
        if parent_result.requires_manual_review:
            manual_queue.append({"document_id": ident, "parent_document_id": parent, "stage": "parent_classification", "reason": parent_result.classification_reasons, "review_status": "pending"})
        output.append(record)

    dump(CLEANED_V22 / "pilot_100_results.json", output)
    dump(CLEANED_V22 / "pilot_100_manual_review_queue.json", manual_queue)
    parent_review = _review_parent_30(output)
    segment_review = _review_segment_30(output)
    dump(REPORTS_V22 / "manual_review_parent_30.json", parent_review)
    dump(REPORTS_V22 / "manual_review_segment_30.json", segment_review)
    _write_review_markdown(REPORTS_V22 / "manual_review_parent_30.md", "V2.2 独立父文档分类审核包（30条）", parent_review, True)
    _write_review_markdown(REPORTS_V22 / "manual_review_segment_30.md", "V2.2 子章节边界审核包（30条）", segment_review, False)

    before = {item["document_id"]: item for item in json.loads(BEFORE_RESULTS.read_text(encoding="utf-8"))}
    classification_changes, section_changes = [], []
    for record in output:
        old = before.get(record["document_id"], {})
        new = {key: record[key] for key in ["parent_document_type", "segment_type", "accident_category", "content_role"]}
        old_view = {"primary_type": old.get("primary_type", ""), "category": old.get("category", "")}
        if old_view != new:
            classification_changes.append({"document_id": record["document_id"], "parent_document_id": record["parent_document_id"], "title": record["title"], "before": old_view, "after": new, "review_status": "pending"})
        old_headings = [item["heading"] for item in old.get("sections", [])]
        new_headings = [item["heading"] for item in record["sections"]]
        if old_headings != new_headings:
            section_changes.append({"document_id": record["document_id"], "parent_document_id": record["parent_document_id"], "before_headings": old_headings, "after_headings": new_headings, "review_status": "pending"})
    dump(REPORTS_V22 / "classification_changes_v2_2.json", classification_changes)
    dump(REPORTS_V22 / "section_split_changes_v2_2.json", section_changes)

    confidence_audit = []
    for record in output:
        strong = [item for item in record["classification_evidence"] if item.get("strength") == "strong"]
        violations = []
        if not record["classification_evidence"] and record["classification_confidence"] > 0.3: violations.append("empty_evidence_high_confidence")
        if record["classification_confidence"] >= 0.8 and len({item["kind"] for item in strong}) < 2: violations.append("high_confidence_without_two_strong_evidence")
        if "规则证据不足" in record["classification_reasons"] and (record["classification_confidence"] >= 0.8 or not record["requires_manual_review"]): violations.append("insufficient_rule_evidence_not_queued")
        confidence_audit.append({"document_id": record["document_id"], "parent_document_id": record["parent_document_id"], "confidence": record["classification_confidence"], "evidence_count": len(record["classification_evidence"]), "strong_evidence_count": len(strong), "requires_manual_review": record["requires_manual_review"], "violations": violations, "review_status": "pending"})
    dump(REPORTS_V22 / "confidence_audit_v2_2.json", confidence_audit)

    fake_heading_patterns = [CAS_RE, PHONE_RE, re.compile(r"[\d.\s]+"), re.compile(r"(?:总指挥|联系人|负责人)\s*[:：].+"), re.compile(r"\d+号）?")]
    headings = [(record["document_id"], section["heading"]) for record in output for section in record["sections"]]
    fake = [(ident, heading) for ident, heading in headings if any(pattern.fullmatch(heading) for pattern in fake_heading_patterns) or re.search(r"\d+(?:万|亿)?元(?:以上|以下)", heading)]
    parent_counts = Counter(parent_classifications[parent].parent_document_type for parent in parent_classifications)
    segment_counts = Counter(record["segment_type"] for record in output)
    sds_count = sum(result.parent_document_type == "sds" for result in parent_classifications.values())
    before_sds = {ident for ident, item in before.items() if item.get("primary_type") == "sds"}
    recovered_sds = [record["document_id"] for record in output if record["parent_document_type"] == "sds" and record["document_id"] not in before_sds]
    empty_high = sum(bool(item["violations"]) for item in confidence_audit)

    sds_lines = ["# V2.2 SDS 恢复报告", "", f"- 独立SDS父文档：{sds_count}", f"- V2.1非SDS、V2.2恢复为SDS的记录：{len(recovered_sds)}", f"- 恢复记录：{recovered_sds}", "", "供应商缺失和章节顺序异常不再单独导致拒绝；PubChem聚合资料不计正式SDS。所有结果仍需人工审核。"]
    (REPORTS_V22 / "sds_recovery_report_v2_2.md").write_text("\n".join(sds_lines) + "\n", encoding="utf-8")
    test_output = (REPORTS_V22 / "pytest_v2_2.txt").read_text(encoding="utf-8") if (REPORTS_V22 / "pytest_v2_2.txt").exists() else "测试输出将在pytest执行后刷新"
    (REPORTS_V22 / "regression_test_report_v2_2.md").write_text("# V2.2 回归测试报告\n\n```text\n" + test_output.strip() + "\n```\n\n测试失败时不得接受试运行结果。\n", encoding="utf-8")
    report = [
        "# V2.2 同一100条试运行报告", "", f"固定种子：`{PILOT_SEED}`；样本来自V2.1锁定清单，未重新抽样。",
        "", "## 分类", "", f"- 独立父文档：{len(parent_classifications)}", f"- 父文档类型：{dict(parent_counts)}",
        f"- 子章节类型：{dict(segment_counts)}", f"- SDS父文档：{sds_count}", f"- SDS误拒绝恢复记录：{len(recovered_sds)}",
        f"- 企业预案：{parent_counts['enterprise_plan']}；企业集团预案：{parent_counts['enterprise_group_plan']}；事业单位预案：{parent_counts['organization_plan']}",
        "", "## 质量", "", f"- 空证据/证据不足但高置信度违规：{empty_high}", f"- 自动扫描假标题：{len(fake)}",
        f"- 父文档审核包：{len(parent_review)}个不同父文档", f"- 子章节审核包：{len(segment_review)}条；同一父文档最多2条",
        "", "## 验收状态", "", "父文档准确率与子章节边界准确率必须由两套人工审核包分别计算。本轮未假装完成人工审核，不具备全量运行条件。",
    ]
    (REPORTS_V22 / "pilot_100_report_v2_2.md").write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
