from __future__ import annotations
import hashlib,re
from presidio_analyzer import Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig
from .models import RedactionAudit

PATTERNS=[("PHONE_CN",r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-— ]?\d{7,8})(?!\d)","{{phone}}"),("EMAIL",r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}","{{email}}"),("ID_CN",r"\b\d{17}[\dXx]\b","{{id_number}}"),("VEHICLE_CN",r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5}","{{vehicle_number}}"),("INTERNAL_DOC",r"(?:应急预案编号|文件编号)\s*[:：]\s*[A-Za-z0-9_-]{3,}","{{internal_document_number}}")]
def redact(document_id: str,text: str):
    findings=[]
    for entity,pattern,replacement in PATTERNS:
        recognizer=PatternRecognizer(supported_entity=entity,patterns=[Pattern(name=entity,regex=pattern,score=.9)],supported_language="zh")
        findings += [(r.start,r.end,entity,replacement,.9) for r in recognizer.analyze(text,entities=[entity],nlp_artifacts=None)]
    # Explicit contact/address fields are Chinese custom rules; audit stores only a hash, never the source value.
    for label,pattern,replacement,group in [("PERSON_CN",r"(?:联系人|总指挥|负责人)\s*[:：]\s*([\u4e00-\u9fff]{2,4})","{{person_name}}",1),("ADDRESS_CN",r"(?:地址|厂址|办公地址)\s*[:：]\s*([^\n。；;]{5,80})","{{address}}",1),("COMPANY_CN",r"([\u4e00-\u9fff]{2,30}(?:有限责任公司|有限公司|股份有限公司))","{{company_name}}",1)]:
        findings += [(m.start(group),m.end(group),label,replacement,.85) for m in re.finditer(pattern,text)]
    findings=sorted(findings,key=lambda x:x[0],reverse=True); audits=[]; result=text
    for start,end,entity,replacement,score in findings:
        original=result[start:end]; result=result[:start]+replacement+result[end:]
        audits.append(RedactionAudit(document_id=document_id,entity_type=entity,original_hash=hashlib.sha256(original.encode()).hexdigest(),replacement=replacement,start=start,end=end,recognizer="presidio_pattern" if entity in {x[0] for x in PATTERNS} else "chinese_custom_regex",score=score))
    return result,list(reversed(audits))
