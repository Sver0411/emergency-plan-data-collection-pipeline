from __future__ import annotations

import json
from pathlib import Path

from pipeline_v2.classification_v2421 import (
    classify_segment_v2421,
    has_full_document_structure,
    has_onsite_disposal_structure,
    is_strict_toc_fragment,
    select_segment_classification_text,
)


FULL_TEXT = """
某市危险化学品生产安全事故应急预案
目 录
1 总则 ........................................ 1
2 组织机构 .................................... 5
3 应急响应 .................................... 9
一、编制目的
为了规范事故处置，制定本预案。发生事故后应当立即组织救援。
二、适用范围
本预案适用于辖区危险化学品事故，相关单位必须遵照执行。
三、风险分析
危险化学品可能发生泄漏、火灾和中毒，事故可能造成人员伤亡。
四、组织机构和职责
指挥部负责统一组织、协调和指挥，各成员单位按照职责开展工作。
五、应急响应
接到报告后立即启动响应，组织警戒、疏散、救援和现场处置。
六、应急结束
现场风险消除并经确认后终止响应，开展总结评估。
"""


def test_full_document_with_front_toc_stays_full_document():
    record = {"raw_text": FULL_TEXT, "normalized_text": "目录页", "segment_type": "toc_fragment", "title": "某市危险化学品生产安全事故应急预案"}
    result, _ = classify_segment_v2421(record)
    assert result == "full_document"


def test_parent_normalized_toc_cannot_override_segment_raw_text():
    record = {"raw_text": FULL_TEXT, "normalized_text": "1 总则 ........ 1\n2 组织机构 ........ 5", "segment_type": "toc_fragment"}
    text, source, parent_used = select_segment_classification_text(record)
    assert text == FULL_TEXT
    assert source == "raw_text"
    assert parent_used is False


def test_local_disposal_body_is_not_toc_fragment():
    text = "1.3 易燃液体事故现场处置方案要点\n遇事故应采取以下措施。首先切断火源；其次组织疏散、警戒和扑救。1、立即堵漏并救援。2、转移危险物料。"
    assert not is_strict_toc_fragment(text)


def test_toc_title_with_real_raw_body_uses_body_structure():
    record = {
        "raw_text": "1.3 易燃液体事故现场处置方案要点\n发生泄漏后立即报警。首先疏散人员并设置警戒；其次组织堵漏、扑救和救援。1、切断火源。2、转移物料。",
        "normalized_text": "1.3 易燃液体事故现场处置方案要点 ........ 125",
        "segment_type": "toc_fragment",
        "original_title": "1.3 易燃液体事故现场处置方案要点",
    }
    result, audit = classify_segment_v2421(record)
    assert result == "onsite_disposal_plan"
    assert audit["segment_classification_text_source"] == "raw_text"


def test_full_plan_not_list_because_original_title_is_list_sentence():
    record = {"raw_text": FULL_TEXT, "original_title": "（4）组织制定并实施安全事故应急预案；", "segment_type": "list_fragment"}
    result, _ = classify_segment_v2421(record)
    assert result == "full_document"


def test_toc_fragment_must_be_mostly_navigation_lines():
    text = "目录\n1 总则 ................................ 1\n2 组织机构 ............................ 5\n3 应急响应 ............................ 9\n4 应急保障 ........................... 12"
    assert is_strict_toc_fragment(text)


def test_full_document_may_contain_toc_and_body():
    assert has_full_document_structure(FULL_TEXT)
    assert not is_strict_toc_fragment(FULL_TEXT)


def test_onsite_actions_take_priority_over_toc_context():
    text = "1.2 压缩或液化气体火灾事故现场处置方案要点\n遇火灾应采取以下措施。1、立即报警并疏散。2、设置警戒并扑救。3、切断气源并组织堵漏救援。"
    assert has_onsite_disposal_structure("1.2 压缩或液化气体火灾事故现场处置方案要点", text)


def test_fixed_output_pending_and_parent_types_unchanged():
    output = Path("cleaned_v2_4_2_1/pilot_100_results_v2_4_2_1.json")
    if not output.exists():
        return
    rows = json.loads(output.read_text(encoding="utf-8"))
    parents = json.loads(Path("cleaned_v2_4_2_1/parent_documents_v2_4_2_1.json").read_text(encoding="utf-8"))
    parent_types = {item["parent_document_id"]: item["parent_document_type"] for item in parents}
    assert all(item["review_status"] == "pending" for item in rows)
    assert len(parent_types) == 49
    assert all(item["parent_document_type"] == parent_types[item["parent_document_id"]] for item in rows)


def test_known_hotfix_records_after_run():
    output = Path("cleaned_v2_4_2_1/pilot_100_results_v2_4_2_1.json")
    if not output.exists():
        return
    rows = {item["document_id"]: item for item in json.loads(output.read_text(encoding="utf-8"))}
    assert rows["rec-9ecf38ddcaa5a2a9"]["segment_type"] == "full_document"
    assert rows["rec-b0ce16eb30e1cfdf"]["segment_type"] == "full_document"
    assert rows["rec-2e380e49e08362fa"]["segment_type"] == "full_document"
    assert rows["rec-0d4deab7922281fd"]["segment_type"] == "onsite_disposal_plan"
    assert rows["rec-b2725e7567fc32aa"]["segment_type"] == "onsite_disposal_plan"
    assert rows["rec-9fa62ab55d92f636"]["segment_type"] == "full_document"
