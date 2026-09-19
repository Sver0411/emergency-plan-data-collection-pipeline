from pipeline_v2.section_splitter import split_sections, is_toc_fragment
def test_cas_phone_and_contact_not_headings():
    text='1 事故风险分析\n正文内容足够长。\n7647-01-0\n13800138000\n联系人：张三\n第2页/共3页\n2 应急处置\n继续处置正文内容足够长。'
    headings=[x.heading for x in split_sections(text)]
    assert headings == ['1 事故风险分析','2 应急处置']
def test_confined_boundary_stops_other_topic():
    text='1 有限空间风险\n有限空间正文足够长。\n2 自然灾害\n不应进入有限空间专项。\n3 防洪防汛\n不应进入。'
    assert len(split_sections(text,'confined_space')) == 1

def test_money_sentences_and_numbered_actions_are_not_headings():
    text='1 事故分级\n正文内容足够长。\n2 1亿元以上直接经济损失的危险化学品事故\n该句属于事故等级描述。\n3 负责组织做好现场警戒并及时报告。\n该句属于职责步骤。\n4 应急响应\n响应正文内容足够长。'
    headings=[x.heading for x in split_sections(text)]
    assert headings == ['1 事故分级','4 应急响应']

def test_full_disposal_sentence_is_not_heading():
    text='1 风险分析\n风险正文内容足够长。\n2 立即切断泄漏源并组织人员撤离；\n该句属于处置步骤。\n3 处置措施\n处置正文内容足够长。'
    assert [x.heading for x in split_sections(text)] == ['1 风险分析','3 处置措施']

def test_document_with_toc_and_body_is_not_toc_fragment():
    text='目录\n1 事故风险分析 ........ 3\n2 应急处置 ........ 8\n3 应急保障 ........ 12\n' + ('本章正文包含完整的风险分析和处置措施。\n' * 300)
    assert is_toc_fragment(text) is False
