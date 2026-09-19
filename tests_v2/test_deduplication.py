from pipeline_v2.deduplication import near_duplicate_groups
def test_duplicate_candidate_not_auto_delete():
    rows=[{'document_id':'a','normalized_text':('危险化学品泄漏专项预案 联系电话13800138000 '*40)},{'document_id':'b','normalized_text':('危险化学品泄漏专项预案 联系电话13900139000 '*40)}]
    groups=near_duplicate_groups(rows)
    assert groups and groups[0]['review_status']=='pending'
