from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from .classification import _evidence, _result
from .classification_v23 import category_v23, classify_v23, segment_type_v23, title_from_parent
from .config import PILOT_SEED, PILOT_SIZE, SOURCE_RECORDS
from .knowledge_extractor import extract_knowledge
from .normalization import CAS_RE, PHONE_RE, normalize_text
from .sds_matcher import sds_from_text
from .section_splitter import split_sections

ROOT = SOURCE_RECORDS.parent.parent
CLEANED_V22 = ROOT / "cleaned_v2"
CLEANED_V23 = ROOT / "cleaned_v2_3"
REPORTS = ROOT / "reports_v2"
REPORTS_V23 = ROOT / "reports_v2_3"
LOCKED = REPORTS / "pilot_sample_manifest_locked_v2_1.json"
BEFORE = ROOT / "cleaned_v2_2" / "pilot_100_results.json"


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def rid(index: int, row: dict) -> str:
    seed = f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def pid(row: dict) -> str:
    identity = row.get("file") or row.get("url") or row.get("title") or "missing-parent"
    return "file-" + hashlib.sha256(str(identity).encode()).hexdigest()[:16]


def locked(rows: list[dict]) -> list[tuple[int, dict, str]]:
    manifest = json.loads(LOCKED.read_text(encoding="utf-8"))
    by_id = {rid(index, row): (index, row) for index, row in enumerate(rows)}
    selected = [(by_id[item["document_id"]][0], by_id[item["document_id"]][1], item.get("sampling_bucket", "locked")) for item in manifest["records"]]
    if len(selected) != PILOT_SIZE:
        raise RuntimeError(f"locked sample changed: {len(selected)}")
    return selected


def parent_context(items: list[tuple[int, dict, str]], parent: str) -> tuple[str, str, str]:
    parsed = CLEANED_V22 / "parsed_documents" / f"{parent}.json"
    if parsed.exists():
        payload = json.loads(parsed.read_text(encoding="utf-8"))
        parsed_text = payload.get("raw_text") or ""
    else:
        parsed_text = ""
    titles, contents, source_types = [], [], []
    for _, row, _ in items:
        title = (row.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
        content = normalize_text(row.get("content") or "")
        if content and content not in contents:
            contents.append(content)
        if row.get("type"):
            source_types.append(row["type"])
    text = parsed_text if len(parsed_text) >= max((len(x) for x in contents), default=0) else "\n\n".join(contents)
    return "｜".join(titles)[:5000], text[:160000], Counter(source_types).most_common(1)[0][0] if source_types else ""


def extract_corrected_segment(parent_text: str, title: str) -> str:
    start = parent_text.find(title)
    if start < 0:
        return ""
    tail = parent_text[start:]
    next_heading = re.search(r"(?m)^\s*(?:\d+|[一二三四五六七八九十]+)[、.．]\s*[^\n]{4,80}(?:专项应急预案|现场处置方案)", tail[3:])
    if next_heading:
        return tail[:next_heading.start() + 3].strip()
    return tail.strip()


def recover_content_heading(text: str) -> str:
    """Use the first complete heading in a parsed child slice, not its source prefix."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    candidate = lines[0]
    if re.match(r"^(?:第?[一二三四五六七八九十百]+[、.]|\d+(?:\.\d+)*[、.．]?)", candidate) and len(candidate) < 120:
        index = 1
        while index < len(lines) and len(candidate) < 120 and not re.search(r"(?:事故|事件|方案|预案)[）)]?$", candidate):
            candidate += lines[index]
            index += 1
    if len(candidate) <= 120 and not re.search(r"[。；;]$", candidate):
        return candidate
    return ""


def write_review(path: Path, title: str, records: list[dict], parent_mode: bool) -> None:
    lines = [f"# {title}", "", "所有条目均为 `pending`，请人工填写空白字段；本程序不代替人工验收。", ""]
    for number, item in enumerate(records, 1):
        key = item["parent_document_id"] if parent_mode else item["document_id"]
        lines += [f"## {number}. {key}", "", f"- 文件：{item['file_name']}", f"- 来源：{item['source_url'] or '缺失'}"]
        if parent_mode:
            lines += [f"- V2.3父文档类型：{item['parent_document_type']}", f"- 内容角色：{item['content_role']}", f"- 置信度：{item['classification_confidence']}", f"- 证据：{json.dumps(item['classification_evidence'], ensure_ascii=False)}", "- 人工父文档类型：________", "- 人工备注：________"]
        else:
            lines += [f"- 父文档类型：{item['parent_document_type']}", f"- 子章节类型：{item['segment_type']}", f"- 事故类别：{item['accident_category']}", f"- 标题：{item['title']}", f"- 章节边界：{json.dumps(item['section_boundaries'], ensure_ascii=False)}", "- 人工子章节类型：________", "- 边界是否正确：________", "- 假标题：________", "- 人工备注：________"]
        lines += [""]
    path.write_text("\n".join(lines), encoding="utf-8")


def parent_review(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for record in records:
        groups[record["parent_document_id"]].append(record)
    representatives = []
    for parent, values in groups.items():
        item = max(values, key=lambda value: len(value["normalized_text"]))
        representatives.append({"parent_document_id": parent, "representative_document_id": item["document_id"], "file_name": Path(item["source_file"]).name, "source_url": item["source_url"], "parent_document_type": item["parent_document_type"], "content_role": item["content_role"], "classification_confidence": item["classification_confidence"], "classification_evidence": item["classification_evidence"], "segment_count_in_pilot": len(values), "human_parent_type": "", "human_notes": "", "review_status": "pending"})
    rng = random.Random(PILOT_SEED + 2301)
    by_type = defaultdict(list)
    for item in representatives:
        by_type[item["parent_document_type"]].append(item)
    selected = []
    for kind in sorted(by_type):
        rng.shuffle(by_type[kind])
        selected.append(by_type[kind].pop())
    rest = [item for values in by_type.values() for item in values]
    rng.shuffle(rest)
    selected.extend(rest[:max(0, 30 - len(selected))])
    return selected[:30]


def segment_review(records: list[dict]) -> list[dict]:
    rng = random.Random(PILOT_SEED + 2302)
    candidates = records[:]
    rng.shuffle(candidates)
    candidates.sort(key=lambda item: (item["segment_type"] == "full_document", item["segment_type"] == "reference_sentence", not item["sections"]))
    counts, selected = Counter(), []
    for item in candidates:
        if counts[item["parent_document_id"]] >= 2:
            continue
        selected.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "file_name": Path(item["source_file"]).name, "source_url": item["source_url"], "parent_document_type": item["parent_document_type"], "segment_type": item["segment_type"], "accident_category": item["accident_category"], "content_role": item["content_role"], "title": item["title"], "section_boundaries": [{"heading": section["heading"], "start": section["start_position"], "end": section["end_position"]} for section in item["sections"][:12]], "warnings": item["warnings"], "human_segment_type": "", "human_boundary_correct": None, "human_false_headings": [], "human_notes": "", "review_status": "pending"})
        counts[item["parent_document_id"]] += 1
        if len(selected) == 30:
            break
    return selected


def main() -> None:
    CLEANED_V23.mkdir(exist_ok=True)
    REPORTS_V23.mkdir(exist_ok=True)
    rows = json.loads(SOURCE_RECORDS.read_text(encoding="utf-8"))
    selected = locked(rows)
    groups = defaultdict(list)
    for item in selected:
        groups[pid(item[1])].append(item)

    parents = {}
    for parent, items in groups.items():
        title, text, original_type = parent_context(items, parent)
        representative = max(items, key=lambda item: len(item[1].get("content") or ""))[1]
        parents[parent] = classify_v23(parent, title, text, representative.get("url") or "", original_type, parent_text=text, sibling_count=len(items))

    output, review_queue = [], []
    for index, row, bucket in selected:
        ident, parent = rid(index, row), pid(row)
        original_title = (row.get("title") or "").strip()
        raw_text = row.get("content") or ""
        normalized = normalize_text(raw_text)
        parent_result = parents[parent]
        original_segment = segment_type_v23(original_title, normalized, parent_result.parent_document_type, len(groups[parent]), "")
        corrected_title = original_title
        if original_segment != "reference_sentence" or re.search(r"已经.{0,30}(?:同意|批准).{0,20}(?:印发|发布)", original_title + normalized[:300]):
            corrected_title = title_from_parent(original_title, normalized, parent_context(groups[parent], parent)[1])
        effective_text = normalized
        if corrected_title != original_title and original_segment in {"list_fragment", "reference_sentence"}:
            extracted = extract_corrected_segment(parent_context(groups[parent], parent)[1], corrected_title)
            if extracted:
                effective_text = extracted
        if "｜" in corrected_title and original_segment in {"guidance_section", "accident_case_section"}:
            suffix = corrected_title.split("｜", 1)[1].strip()
            if suffix:
                corrected_title = suffix
        if original_segment in {"guidance_section", "accident_case_section"}:
            content_heading = recover_content_heading(effective_text)
            if content_heading and ("｜" in original_title or original_title.startswith(("W", "有限空间作业"))):
                corrected_title = content_heading
        if original_segment == "reference_sentence" and corrected_title == original_title:
            segment_type = "reference_sentence"
        elif original_segment == "reference_sentence" and corrected_title != original_title and re.search(r"(?:专项|综合)?应急预案|现场处置方案", corrected_title):
            # A notice/reference slice is promoted only after a real heading is
            # recovered from the parent parse; the notice itself never remains
            # the segment title.
            segment_type = "embedded_special_section"
        elif original_segment in {"embedded_special_section", "list_fragment"} and corrected_title != original_title and re.search(r"(?:专项|综合)?应急预案|现场处置方案", corrected_title):
            segment_type = "embedded_special_section"
        else:
            segment_type = segment_type_v23(corrected_title, effective_text, parent_result.parent_document_type, len(groups[parent]), parent_context(groups[parent], parent)[1])
        category = category_v23(corrected_title, effective_text)
        role = parent_result.content_role
        if segment_type == "onsite_disposal_plan": role = "disposal_knowledge"
        if segment_type == "list_fragment": role = "reference_sentence"
        if segment_type == "reference_sentence": role = "reference_sentence"
        sections = [] if segment_type in {"list_fragment", "reference_sentence", "toc_fragment"} else split_sections(effective_text, category)
        knowledge = [item.model_dump() for item in extract_knowledge(ident, row.get("url") or "", sections)]
        sds = sds_from_text(ident, effective_text, row.get("url") or "") if parent_result.parent_document_type == "sds" else None
        warnings = []
        if corrected_title != original_title: warnings.append("title_recovered_from_parent_parse")
        if original_segment in {"list_fragment", "reference_sentence"}: warnings.append("original_slice_was_list_or_reference_sentence")
        if parent_result.requires_manual_review: warnings.append("parent_classification_requires_manual_review")
        quality = "low_quality" if parent_result.parent_document_type == "sds" and parent_result.requires_manual_review else "manual_review" if parent_result.requires_manual_review else "silver_candidate"
        record = {"document_id": ident, "parent_document_id": parent, "source_file": row.get("file") or "[missing]", "source_url": row.get("url") or "", "title": corrected_title, "original_title": original_title, "raw_text": raw_text, "normalized_text": effective_text, "parent_document_type": parent_result.parent_document_type, "segment_type": segment_type, "accident_category": category, "content_role": role, "classification_reasons": parent_result.classification_reasons, "classification_evidence": parent_result.classification_evidence, "classification_confidence": parent_result.classification_confidence, "requires_manual_review": parent_result.requires_manual_review, "sections": [section.model_dump() for section in sections], "knowledge_items": knowledge, "sds": sds.model_dump() if sds else None, "warnings": warnings, "sampling_bucket": bucket, "quality_grade": quality, "review_status": "pending"}
        output.append(record)
        if parent_result.requires_manual_review:
            review_queue.append({"document_id": ident, "parent_document_id": parent, "stage": "parent_classification", "reason": parent_result.classification_reasons, "review_status": "pending"})

    dump(CLEANED_V23 / "pilot_100_results.json", output)
    dump(CLEANED_V23 / "pilot_100_manual_review_queue.json", review_queue)
    p_review, s_review = parent_review(output), segment_review(output)
    dump(REPORTS_V23 / "manual_review_parent_30.json", p_review)
    dump(REPORTS_V23 / "manual_review_segment_30.json", s_review)
    write_review(REPORTS_V23 / "manual_review_parent_30.md", "V2.3 独立父文档分类审核包（30条）", p_review, True)
    write_review(REPORTS_V23 / "manual_review_segment_30.md", "V2.3 子章节边界审核包（30条）", s_review, False)

    before = {item["document_id"]: item for item in json.loads(BEFORE.read_text(encoding="utf-8"))}
    type_changes, section_changes = [], []
    for item in output:
        old = before.get(item["document_id"], {})
        new = {key: item[key] for key in ["parent_document_type", "segment_type", "accident_category", "content_role"]}
        old_view = {"parent_document_type": old.get("parent_document_type", ""), "segment_type": old.get("segment_type", ""), "accident_category": old.get("accident_category", ""), "content_role": old.get("content_role", "")}
        if old_view != new or item["title"] != old.get("title", ""):
            type_changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before": {**old_view, "title": old.get("title", "")}, "after": {**new, "title": item["title"]}, "review_status": "pending"})
        old_heads = [section["heading"] for section in old.get("sections", [])]
        new_heads = [section["heading"] for section in item["sections"]]
        if old_heads != new_heads:
            section_changes.append({"document_id": item["document_id"], "parent_document_id": item["parent_document_id"], "before_headings": old_heads, "after_headings": new_heads, "review_status": "pending"})
    dump(REPORTS_V23 / "classification_changes_v2_3.json", type_changes)
    dump(REPORTS_V23 / "section_split_changes_v2_3.json", section_changes)

    sds_records = [item for item in output if item["parent_document_type"] == "sds"]
    low_sds = [item["document_id"] for item in sds_records if item["quality_grade"] == "low_quality"]
    (REPORTS_V23 / "sds_quality_report_v2_3.md").write_text("# V2.3 SDS质量分级报告\n\n" + f"- SDS记录数：{len(sds_records)}\n- 低质量但保留人工审核：{len(low_sds)}\n- 低质量记录：{low_sds}\n- 规则：存在SDS标识、化学品身份和至少4类章节时，排版噪声不直接拒绝；所有结果保持 `pending`。\n", encoding="utf-8")

    headings = [(item["document_id"], section["heading"]) for item in output for section in item["sections"]]
    fake = []
    for ident, heading in headings:
        phrase = re.sub(r"^(?:第[一二三四五六七八九十百]+[章节]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+){0,4}[、.．]?)\s*", "", heading)
        if CAS_RE.fullmatch(heading) or PHONE_RE.fullmatch(heading) or re.fullmatch(r"[\d.\s]+", heading) or re.search(r"\d+(?:万|亿)?元(?:以上|以下)", heading) or re.match(r"^(?:负责|组织|发生|按照|根据|立即|开展|编制|发布|修订)", phrase) or heading.endswith(("。", "；", ";")):
            fake.append({"document_id": ident, "heading": heading})
    dump(REPORTS_V23 / "fake_heading_audit_v2_3.json", fake)
    (REPORTS_V23 / "fake_heading_audit_v2_3.md").write_text("# V2.3假标题审计报告\n\n" + f"自动发现候选假标题：{len(fake)}条。\n\n" + "\n".join(f"- `{item['document_id']}`：{item['heading']}" for item in fake), encoding="utf-8")

    cat_counts = Counter(item["accident_category"] for item in output)
    target_categories = {
        item["document_id"]: item["accident_category"]
        for item in output
        if before.get(item["document_id"], {}).get("accident_category") != item["accident_category"]
    }
    (REPORTS_V23 / "accident_category_audit_v2_3.md").write_text("# V2.3事故类别审计报告\n\n" + f"- 类别分布：{dict(cat_counts)}\n- 定点记录结果：{json.dumps(target_categories, ensure_ascii=False)}\n- 事故类别准确率：待人工审核后统计。\n", encoding="utf-8")

    test_text = (REPORTS_V23 / "pytest_v2_3.txt").read_text(encoding="utf-8") if (REPORTS_V23 / "pytest_v2_3.txt").exists() else "测试输出待刷新"
    (REPORTS_V23 / "regression_test_report_v2_3.md").write_text("# V2.3回归测试报告\n\n```text\n" + test_text.strip() + "\n```\n", encoding="utf-8")
    parents_count = Counter(item.parent_document_type for item in parents.values())
    segment_count = Counter(item["segment_type"] for item in output)
    report = ["# V2.3固定100条试运行报告", "", f"固定种子：`{PILOT_SEED}`；沿用V2.1锁定清单，未重新抽样。", "", "## 结果", "", f"- 记录数：{len(output)}", f"- 独立父文档：{len(parents)}", f"- 父文档类型：{dict(parents_count)}", f"- 子章节类型：{dict(segment_count)}", f"- SDS：{len(sds_records)}；低质量SDS：{len(low_sds)}", f"- 分类变化：{len(type_changes)}；章节变化：{len(section_changes)}", f"- 假标题候选：{len(fake)}", "", "## 独立验收指标", "", "- 父文档准确率：待父文档审核包人工完成后统计。", "- 子章节类型准确率：待子章节审核包人工完成后统计。", "- 子章节边界准确率：待子章节审核包人工完成后统计。", "- 事故类别准确率：待事故类别人工复核后统计。", "- SDS识别准确率：待SDS人工复核后统计。", "", "本轮仅完成定点规则修复和固定100条验证，所有结果保持 `pending`，不具备全量运行条件。"]
    (REPORTS_V23 / "pilot_100_report_v2_3.md").write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
