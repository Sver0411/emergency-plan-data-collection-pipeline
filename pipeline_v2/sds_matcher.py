from __future__ import annotations
import re
from .models import SDSRecord

def sds_from_text(document_id: str, text: str, source_url: str) -> SDSRecord:
    cn=re.search(r"(?:化学品中文名称|产品名称|中文名称)\s*[:：]\s*([^\n]{1,60})",text)
    en=re.search(r"(?:英文名称|化学品英文名)\s*[:：]\s*([^\n]{1,80})",text,re.I)
    cas=re.search(r"CAS(?:号| No\.?|编号)?\s*[:： ]\s*(\d{2,7}-\d{2}-\d)",text,re.I)
    mixture=bool(re.search(r"□?混合物|混合物",text)); pure=bool(re.search(r"☑?纯品|\b物质\b",text))
    result=SDSRecord(document_id=document_id,chemical_name_cn=cn.group(1).strip() if cn else "",chemical_name_en=en.group(1).strip() if en else "",cas=cas.group(1) if cas else "",is_mixture=True if mixture else (False if pure else None),source_urls=[source_url] if source_url else [],source_sections=[])
    if result.is_mixture and not re.search(r"(?:组分|成分|组成信息)",text): result.conflict_notes.append("混合物但未识别到组分表，需人工审核")
    if result.chemical_name_cn and result.cas and result.chemical_name_cn in {"甲醇","苯","乙醇"} and result.chemical_name_cn not in text[:2000]: result.conflict_notes.append("名称、CAS与正文位置不一致，需人工审核")
    return result
