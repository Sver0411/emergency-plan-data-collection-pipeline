from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path
import pytest

from pipeline_v2.classification_v23 import (
    category_v23,
    classify_v23,
    segment_type_v23,
    title_from_parent,
)
from pipeline_v2.normalization import normalize_text
from pipeline_v2.section_splitter import split_sections


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "json" / "records.json").exists():
    pytest.skip("requires private source records", allow_module_level=True)
ROWS = json.loads((ROOT / "json" / "records.json").read_text(encoding="utf-8"))


def record_id(index: int, row: dict) -> str:
    seed = f"{index}|{row.get('file','')}|{row.get('url','')}|{row.get('title','')}|{row.get('content','')[:300]}"
    return "rec-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


BY_ID = {record_id(index, row): (index, row) for index, row in enumerate(ROWS)}


def parent_text(row: dict) -> str:
    parent = "file-" + hashlib.sha256(str(row.get("file") or row.get("url") or row.get("title") or "missing-parent").encode()).hexdigest()[:16]
    parsed = ROOT / "cleaned_v2" / "parsed_documents" / f"{parent}.json"
    if parsed.exists():
        return json.loads(parsed.read_text(encoding="utf-8")).get("raw_text", "")
    return row.get("content", "")


def classify_target(target: str):
    _, row = BY_ID[target]
    text = parent_text(row)
    parent = "file-" + hashlib.sha256(str(row.get("file") or row.get("url") or row.get("title") or "missing-parent").encode()).hexdigest()[:16]
    return classify_v23(parent, row.get("title", ""), text, row.get("url", ""), row.get("type", ""), parent_text=text, sibling_count=3)


def test_parent_regression_by_source_content():
    # These assertions select records by their stable source content, so the
    # implementation cannot answer by embedding the regression IDs.
    checks = [
        ("煤矿安全规程", "regulation"),
        ("工贸企业有限空间作业安全规定", "regulation"),
        ("关于加强化工企业泄漏管理的指导意见", "guidance_reference"),
        ("首批重点监管的危险化学品安全措施和应急处置原则", "guidance_reference"),
    ]
    selectors = {
        "煤矿安全规程": lambda row: "t20250804_553279" in row.get("file", ""),
        "工贸企业有限空间作业安全规定": lambda row: "工贸企业有限空间作业安全规定" in (row.get("title", "") + row.get("content", "")),
        "关于加强化工企业泄漏管理的指导意见": lambda row: "关于加强化工企业泄漏管理的指导意见" in row.get("title", ""),
        "首批重点监管的危险化学品安全措施和应急处置原则": lambda row: "首批重点监管的危险化学品安全措施和应急处置原则" in row.get("title", ""),
    }
    for marker, expected in checks:
        row = next(row for row in ROWS if selectors[marker](row))
        text = parent_text(row)
        parent = "file-" + hashlib.sha256(str(row.get("file") or row.get("url") or row.get("title") or "missing-parent").encode()).hexdigest()[:16]
        result = classify_v23(parent, row.get("title", ""), text, row.get("url", ""), row.get("type", ""), parent_text=text, sibling_count=2)
        assert result.parent_document_type == expected
    row = next(row for row in ROWS if "首批重点监管的危险化学品安全措施和应急处置原则" in row.get("title", ""))
    text = parent_text(row)
    parent = "file-" + hashlib.sha256(str(row.get("file") or row.get("url") or row.get("title") or "missing-parent").encode()).hexdigest()[:16]
    assert classify_v23(parent, row.get("title", ""), text, row.get("url", ""), row.get("type", ""), parent_text=text, sibling_count=2).content_role == "chemical_safety_reference"


def test_child_regression_types_and_categories():
    _, transport = BY_ID["rec-5546334f6f91e834"]
    assert segment_type_v23(transport["title"], transport["content"], "enterprise_group_plan", 2) == "onsite_disposal_plan"
    assert category_v23(transport["title"], transport["content"]) == "transport_accident"

    for target in ("rec-a36e466db102df32", "rec-f1a05239ae5db834"):
        _, row = BY_ID[target]
        assert segment_type_v23(row["title"], row["content"], "guidance_reference", 2) == "accident_case_section"
        assert category_v23(row["title"], row["content"]) == "poisoning_asphyxia"

    _, natural = BY_ID["rec-090f60df96164dc6"]
    assert segment_type_v23(natural["title"], natural["content"], "government_or_regional_plan", 2) == "reference_sentence"
    assert category_v23(natural["title"], natural["content"]) == "natural_disaster"

    _, multi_a = BY_ID["rec-5ca5817c03072739"]
    _, multi_b = BY_ID["rec-106ee031fc6b4470"]
    assert category_v23(multi_a["title"], multi_a["content"]) == "multi_hazard"
    assert category_v23(multi_b["title"], multi_b["content"]) == "multi_hazard"


def test_heading_recovery_and_no_half_sentence_titles():
    for target, marker in [
        ("rec-04eba8f63269599e", "天然气保供突发事件专项应急预案"),
        ("rec-693cc4f85670c50e", "重庆市荣昌区危险化学品事故专项应急预案"),
        ("rec-f84100a487ae4497", "上海市杨浦区处置危险化学品生产安全事故应急预案"),
    ]:
        _, row = BY_ID[target]
        recovered = title_from_parent(row["title"], row["content"], parent_text(row))
        assert marker in recovered
        assert not recovered.startswith(("（", "(", "已经", "按照"))

    _, duty = BY_ID["rec-b46cc6d5bec686b8"]
    assert segment_type_v23(duty["title"], duty["content"], "government_or_regional_plan", 2) == "list_fragment"
    assert title_from_parent(duty["title"], duty["content"], parent_text(duty)) == duty["title"]


def test_heading_rejection_rules_and_boundary():
    text = """1 总则\n这是有效章节正文。\n2 0.022 50 0.0004\n表格数值。\n3 总指挥：张三\n职责数据。\n4 34号）\n法规残片。\n5 48小时以上\n时间参数。\n6 负责组织人员立即撤离现场并关闭阀门。\n处置句正文。\n7 应急处置措施\n隔离、警戒、疏散。\n8 自然灾害专项应急预案\n自然灾害正文。\n9 有限空间专项应急预案\n有限空间正文。"""
    headings = [section.heading for section in split_sections(text, "confined_space")]
    assert "1 总则" in headings
    assert "7 应急处置措施" in headings
    assert all("0.022" not in heading for heading in headings)
    assert all("总指挥" not in heading for heading in headings)
    assert all("34号" not in heading for heading in headings)
    assert all("48小时" not in heading for heading in headings)
    assert all("负责组织" not in heading for heading in headings)
    assert "8 自然灾害专项应急预案" not in headings


def test_new_category_and_segment_literals_are_supported():
    assert category_v23("矿井运输事故现场处置方案", "") == "mine_transport_accident"
    assert segment_type_v23("（1）执行某专项应急预案。", "", "government_or_regional_plan", 2) == "reference_sentence"
    assert segment_type_v23("（1）负责组织编制预案。", "", "government_or_regional_plan", 2) == "list_fragment"


def test_production_overlay_contains_no_regression_id_answers():
    from pipeline_v2 import classification_v23

    source = inspect.getsource(classification_v23)
    assert not re.search(r"rec-[0-9a-f]{16}|file-[0-9a-f]{16}", source)
