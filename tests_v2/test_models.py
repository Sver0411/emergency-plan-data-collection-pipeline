import pytest
from pydantic import ValidationError
from pipeline_v2.models import SDSRecord, PlanSection, ClassifiedDocumentV22
def test_bad_cas_rejected():
    with pytest.raises(ValidationError): SDSRecord(document_id='abcdefghi',cas='bad')
def test_invalid_section_range_rejected():
    with pytest.raises(ValidationError): PlanSection(section_id='x',heading='标题',content='正文',start_position=3,end_position=2)
def test_high_confidence_needs_two_strong_evidence_items():
    with pytest.raises(ValidationError):
        ClassifiedDocumentV22(document_id='x',parent_document_type='sds',segment_type='full_document',classification_confidence=0.9,classification_evidence=[{'kind':'title','text':'SDS','start':0,'end':3,'strength':'strong'}],requires_manual_review=False)
