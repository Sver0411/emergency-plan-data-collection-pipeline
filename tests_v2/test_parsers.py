from pipeline_v2.normalization import normalize_text
def test_normalization_keeps_cas():
    assert '7647-01-0' in normalize_text('第 2 页\n7647-01-0\n')
