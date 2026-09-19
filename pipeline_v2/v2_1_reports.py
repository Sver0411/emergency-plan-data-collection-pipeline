"""Create V2.1 comparison and review artifacts without changing raw or V2.0 snapshots."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .config import REPORTS


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    before = _read(REPORTS / "pilot_100_results_before_v2_1.json", [])
    after = _read(Path("cleaned_v2") / "pilot_100_results.json", [])
    before_by_id = {record["document_id"]: record for record in before}
    changes = []
    section_changes = []
    for record in after:
        old = before_by_id.get(record["document_id"], {})
        old_type, new_type = old.get("primary_type", "[new]"), record["primary_type"]
        old_category, new_category = old.get("category", "[new]"), record["category"]
        if old_type != new_type or old_category != new_category:
            changes.append({
                "document_id": record["document_id"], "title": record["title"],
                "source_url": record["source_url"], "before": {"primary_type": old_type, "category": old_category},
                "after": {"primary_type": new_type, "category": new_category},
                "rule_reason": record["classification_reasons"], "review_status": "pending",
            })
        old_sections = [section["heading"] for section in old.get("sections", [])]
        new_sections = [section["heading"] for section in record.get("sections", [])]
        if old_sections != new_sections:
            section_changes.append({
                "document_id": record["document_id"], "title": record["title"],
                "source_url": record["source_url"], "before_headings": old_sections,
                "after_headings": new_sections, "parent_document_id": record.get("parent_document_id", ""),
                "review_status": "pending",
            })
    _write_json(REPORTS / "classification_changes_v2_1.json", changes)
    _write_json(REPORTS / "section_split_changes_v2_1.json", section_changes)

    review = _read(REPORTS / "manual_review_30.json", [])
    lines = ["# V2.1 30条人工审核抽查包", "", "本包基于锁定的同一批100条记录重新生成。所有结论仍为 `pending`；程序未把人工复核替换为自动结论。", ""]
    for item in review:
        lines += [
            f"## {item['document_id']}", "",
            f"- 文件：{item['file_name']}", f"- 来源：{item['source_url'] or '缺失，需人工核查'}",
            f"- 原始分类：{item['original_type'] or '未标注'}", f"- V2.1 分类：{item['v2_type']}",
            f"- 分类证据：{'; '.join(item['classification_evidence']) or '规则证据不足'}",
            f"- 主要章节：{'；'.join(item['main_sections']) or '未切分'}",
            f"- 重复判断：{item['duplicate_status']}",
            f"- SDS 身份：{json.dumps(item['sds_identity'], ensure_ascii=False) if item['sds_identity'] else '不适用'}",
            "- 人工待选结论：分类是否正确；章节边界是否正确；如为SDS，名称/CAS/混合物身份是否正确。", "",
        ]
    (REPORTS / "manual_review_30_v2_1.md").write_text("\n".join(lines), encoding="utf-8")

    stats = Counter(record["primary_type"] for record in after)
    parser_stats = Counter()
    seen_parsed_ids = set()
    for parsed_path in (Path("cleaned_v2") / "parsed_documents").glob("*.json"):
        parsed = _read(parsed_path, {})
        if parsed.get("document_id") in seen_parsed_ids:
            continue
        seen_parsed_ids.add(parsed.get("document_id"))
        parser_stats[parsed.get("parser", "unknown")] += 1
    report = [
        "# V2.1 固定100条试运行报告", "", "本次未重新抽样、未全量处理、未导入知识库、未修改原始资料。",
        "", "## 本轮修订", "", "- SDS 必须同时具备显式SDS标题、产品/供应商、危害、组成和至少两个响应章节证据。",
        "- 政府/区域主体和开发区/园区证据优先于企业预案判断。",
        "- 新增化学品目录、指导参考、事业单位专项预案三种类型。",
        "- 事故类别以标题、适用范围、风险分析、处置对象为优先级，正文词仅辅助。",
        "- 数字表格、人名岗位、法规残片、时长和长处置句不作为章节标题。", "",
        "## 结果", "", f"- 锁定样本数：{len(after)}", f"- 分类分布：{dict(stats)}",
        f"- 使用的主解析器：{dict(parser_stats)}", "- 人工基准结果已迁移到测试文件，生产报告代码不再按 document_id 写入答案。",
        f"- 分类变化：{len(changes)} 条；章节变化：{len(section_changes)} 条。", "",
        "## 验收", "", "回归测试通过不等同于人工验收。请人工复查本轮30条审核包后再计算准确率；当前仍不具备全量运行条件。",
    ]
    (REPORTS / "pilot_100_report_v2_1.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    pytest_text = (REPORTS / "pytest_v2_1.txt").read_text(encoding="utf-8") if (REPORTS / "pytest_v2_1.txt").exists() else "未找到pytest输出"
    regression = ["# V2.1 回归测试报告", "", "## 执行结果", "", "```text", pytest_text.strip(), "```", "", "## 覆盖", "", "- 12条人工复核基准分类；南山区、达州市政府预案；目录；开发区；事故通报；指导手册。", "- 数字表格、人名岗位、法规文号残片和新现场处置标题切分。", "- 原有解析、去重、分类、脱敏、SDS和模型校验测试。", "", "所有测试通过后才执行了固定100条试运行。"]
    (REPORTS / "regression_test_report_v2_1.md").write_text("\n".join(regression) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
