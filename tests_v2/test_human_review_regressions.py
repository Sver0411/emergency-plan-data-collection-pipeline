import json
from pathlib import Path
import pytest

from pipeline_v2.classification import classify, segment_type_of
from pipeline_v2.section_splitter import split_sections

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "cleaned_v2/pilot_100_results.json").exists():
    pytest.skip("requires private pilot output", allow_module_level=True)
PILOT = json.loads((ROOT / "cleaned_v2/pilot_100_results.json").read_text(encoding="utf-8"))
BY_ID = {item["document_id"]: item for item in PILOT}


def classify_real(document_id: str):
    row = BY_ID[document_id]
    siblings = [item for item in PILOT if item["source_file"] == row["source_file"]]
    parent_text = "\n\n".join(item["normalized_text"] for item in siblings)
    return classify(document_id, row["title"], row["normalized_text"], row["source_url"], "", parent_text=parent_text, sibling_count=len(siblings))


def test_government_plan_not_sds():
    text='某区人民政府 危险化学品事故应急预案 成员单位职责 辖区企业 区域资源调度 安全技术说明书'
    assert classify('x','南山区危险化学品事故应急预案',text,'u','emergency_plan').parent_document_type == 'government_or_regional_plan'


def test_dazhou_government_plan_not_sds():
    text='达州市人民政府 危险化学品事故应急预案 成员单位职责 请求上级政府支援 区域资源调度'
    assert classify('x','达州市危险化学品事故应急预案',text,'u','emergency_plan').parent_document_type == 'government_or_regional_plan'


def test_catalog_not_sds():
    text='资料类型：危险化学品目录条目 目录序号：8 品名：甲醇 CAS号：67-56-1 危险性类别：易燃'
    result=classify('x','危险化学品目录｜甲醇',text,'u','knowledge')
    assert result.parent_document_type == 'chemical_catalog'
    assert result.segment_type == 'table'


def test_development_zone_not_enterprise():
    text='开发区管委会 成员单位职责 辖区企业 区域资源调度 事故发生单位'
    assert classify('x','开发区燃气泄漏专项应急预案',text,'u','emergency_plan').parent_document_type == 'government_or_regional_plan'


def test_guidance_not_accident_case():
    assert classify('x','初始气体检测','有限空间作业指导手册 作业前检测方法 风险防控确认','u','knowledge').parent_document_type == 'guidance_reference'


def test_accident_news_is_accident_case():
    text='3月28日某市发生事故。事故经过：人员进入污水池。造成4人死亡。事故原因分析及事故通报提出教训。'
    assert classify('x','某企业中毒事故通报',text,'u','knowledge').parent_document_type == 'accident_case'


def test_table_number_and_document_fragment_not_heading():
    text='1 事故风险分析\n正文内容足够长。\n0.022 50 0.0004\n34号）\n48小时以上\n2 应急处置\n正文内容足够长。'
    assert [x.heading for x in split_sections(text)] == ['1 事故风险分析','2 应急处置']


def test_new_onsite_title_is_segment_not_parent_type():
    text='本公司生产车间存在有限空间风险，公司应急指挥部负责响应。'
    result=classify('x','2 硫化氢中毒现场处置方案',text,'u','emergency_plan',sibling_count=2)
    assert result.parent_document_type == 'enterprise_plan'
    assert result.segment_type == 'onsite_disposal_plan'


def test_v22_enterprise_and_group_regression_records():
    for ident, parent, segment, category in [
        ('rec-67a5da6cb0166847','enterprise_plan','embedded_special_section','major_hazard'),
        ('rec-79243fded2d7edb1','enterprise_plan','embedded_special_section','chemical_leakage'),
        ('rec-106ee031fc6b4470','enterprise_group_plan','embedded_special_section',None),
        ('rec-424e4b5762d28d57','enterprise_group_plan','embedded_special_section',None),
        ('rec-9fa62ab55d92f636','organization_plan','embedded_special_section',None),
    ]:
        result=classify_real(ident)
        assert result.parent_document_type == parent
        siblings=[x for x in PILOT if x['source_file']==BY_ID[ident]['source_file']]
        actual_segment=segment_type_of(BY_ID[ident]['title'],BY_ID[ident]['normalized_text'],parent,len(siblings))
        assert actual_segment == segment
        if category: assert result.accident_category == category


def test_v22_sds_recovery_and_catalog_records():
    for ident in ['rec-5e237eba1c530459','rec-0cc731c8b18e3080','rec-43873dd8f43850ca']:
        assert classify_real(ident).parent_document_type == 'sds'
    assert classify_real('rec-c1185a5c2de6f3fd').parent_document_type in {'reject','manual_review'}
    assert classify_real('rec-64d001a0f9d234e8').parent_document_type == 'guidance_reference'
    for ident in ['rec-f34ea7834f4ad63b','rec-325191df1aa9895a','rec-ab0e890faf02938f']:
        assert classify_real(ident).parent_document_type == 'chemical_catalog'


def test_v22_guidance_and_accident_case_records():
    assert classify_real('rec-2b457fe605a0dd95').parent_document_type == 'guidance_reference'
    assert classify_real('rec-5ca5817c03072739').parent_document_type == 'guidance_reference'
    assert classify_real('rec-b1788c434c033391').parent_document_type == 'accident_case'


def test_confidence_is_calibrated_without_id_override():
    result=classify('rec-5e237eba1c530459','无特征标题','普通正文'*100,'u','')
    assert result.parent_document_type == 'manual_review'
    assert result.classification_confidence <= 0.3
    assert result.requires_manual_review is True
