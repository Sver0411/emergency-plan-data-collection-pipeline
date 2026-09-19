from pipeline_v2.classification import classify
def test_government_not_enterprise():
    text='某区人民政府成员单位职责，负责辖区企业和区域资源调度。'*100
    x=classify('x','某区危险化学品事故应急预案',text,'https://example.gov.cn','emergency_plan')
    assert x.parent_document_type == 'government_or_regional_plan'
def test_onsite_not_complete_plan():
    text='本公司生产车间硫化氢现场处置，公司应急指挥部负责启动响应。'*100
    x=classify('x','硫化氢现场处置方案',text,'https://example.com','emergency_plan')
    assert x.parent_document_type == 'enterprise_plan'
    assert x.segment_type == 'onsite_disposal_plan'
