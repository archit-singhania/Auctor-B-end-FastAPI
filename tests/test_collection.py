from app.collection import claims, describe


def test_claim_identity_changes_with_source_revision():
    before = claims({'projects': [{'name': 'API', 'description': 'old'}]}, {})
    after = claims({'projects': [{'name': 'API', 'description': 'new'}]}, {})
    assert set(before).isdisjoint(after)
    assert before == claims({'projects': [{'description': 'old', 'name': 'API'}]}, {})


def test_collection_uses_current_eligibility_without_verifying_claims():
    claim = next(iter(claims({'skills': ['Python']}, {}).values()))
    source = dict(id='one', kind='certificate', status='pending', eligibility_state='current')
    assert describe(claim, [source], [])['status'] == 'missing source'
    assert describe(claim, [source], ['one'])['status'] == 'no eligible reviewed source'
    source['status'] = 'verified'
    assert describe(claim, [source], ['one'])['status'] == 'current reviewed source linked'
    source['review_due_at'] = '2000-01-01T00:00:00Z'
    assert describe(claim, [source], ['one'])['status'] == 'source needs re-review'
    source['eligibility_state'] = 'revoked'
    assert describe(claim, [source], ['one'])['status'] == 'linked sources revoked'
    source['kind'] = 'experience'
    assert describe(claim, [source], ['one'])['status'] == 'weak link'
