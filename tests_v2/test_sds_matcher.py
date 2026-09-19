from pipeline_v2.sds_matcher import sds_from_text
def test_methanol_mixture_not_pure():
    x=sds_from_text('x','产品名称：甲醇标准溶液\n混合物\nCAS号：67-56-1','https://example.com')
    assert x.is_mixture is True
def test_missing_cas_stays_empty():
    assert sds_from_text('x','产品名称：乙醇\n安全技术说明书','u').cas == ''
