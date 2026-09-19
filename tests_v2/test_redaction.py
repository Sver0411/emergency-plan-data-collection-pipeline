from pipeline_v2.redaction import redact
def test_phone_but_not_emergency_or_cas():
    text='联系人：张三，电话13800138000，报警119，CAS 67-56-1，苏A12345'
    safe,audit=redact('x',text)
    assert '{{phone}}' in safe and '119' in safe and '67-56-1' in safe and '{{person_name}}' in safe
def test_company_redaction():
    safe,_=redact('x','某某化工有限公司 地址：江苏省某市某路10号')
    assert '{{company_name}}' in safe and '{{address}}' in safe
