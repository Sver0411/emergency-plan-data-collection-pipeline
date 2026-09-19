from __future__ import annotations
from pydantic import ValidationError
from .models import EnterprisePlan, SDSRecord

def validate_record(record: dict) -> list[str]:
    errors=[]
    try:
        if record.get("primary_type") == "sds": SDSRecord.model_validate(record.get("sds", {"document_id":record["document_id"]}))
        else: EnterprisePlan.model_validate({k:record.get(k) for k in ["document_id","title","primary_type","category","plan_level","sections","source_url","review_status"]})
    except ValidationError as exc: errors=[f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return errors
