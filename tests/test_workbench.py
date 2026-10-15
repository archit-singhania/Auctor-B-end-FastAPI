import pytest
from app.workbench import snapshot_text, self_review, PracticePatch

def test_snapshot_limits_are_explicit_and_stable():
    result=snapshot_text({'narrative':'x'*120000})
    assert len(result['text'])<=100000 and result['truncated'] is True
    assert result['text_sha256']==snapshot_text({'narrative':'x'*120000})['text_sha256']

def test_no_text_pdf_does_not_report_success(monkeypatch):
    from app.services.cv_parser import CvParserService
    monkeypatch.setattr(CvParserService,'_extract_text',lambda *_:'')
    with pytest.raises(ValueError):snapshot_text({},b'%PDF-illustrative')

def test_practice_is_only_a_bounded_self_review():
    result=self_review([{'id':'0'}],{'0':'I improved latency by 20 percent.'},{'0':{'outcome':True}})
    assert result['items'][0]['word_count']==6 and result['items'][0]['has_numeric_detail']
    assert 'no badge' in result['scope']
    with pytest.raises(ValueError):PracticePatch(revision=0,review={'0':{'verified':True}})
    with pytest.raises(ValueError):PracticePatch(revision=0,answers={'0':'x'*4001})
