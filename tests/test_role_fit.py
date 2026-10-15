import pytest
from app.role_fit import RoleDocument, evaluate

def test_weighted_coverage_uses_eligible_evidence_and_preserves_unknowns():
    role=RoleDocument(title='Engineer',reviewed=True,requirements=[{'skill':'Docker'},{'skill':'PostgreSQL','priority':'preferred'},{'skill':'Redis','weight':3},{'skill':'Unobserved tool','weight':4}]).model_dump(mode='json')
    cv={'skills':['Postgres'],'projects':[{'name':'Service','tech_stack':['Redis']}]}
    evidence=[{'id':'repo','kind':'project','title':'Repository','status':'verified','detail':{'project':'Service'}}, {'id':'stale','kind':'certificate','title':'Old proof','status':'stale','detail':{'skills':['PostgreSQL']}}]
    result=evaluate(role,cv,evidence,['docker'])
    assert result['coverage_percent']==20 and result['earned_weight']==2 and result['total_weight']==10
    assert [r['status'] for r in result['contributions']]==['supported','declared-only','partial','unknown']
    assert result['contributions'][2]['context'][0]['id']=='repo'
    evidence.append({'id':'cert','kind':'certificate','title':'Reviewed source','status':'verified','detail':{'skills':['Postgres']}})
    updated=evaluate(role,cv,evidence,['docker'])
    assert updated['coverage_percent']==30 and updated['input_sha256']!=result['input_sha256']
    assert updated['contributions'][1]['sources'][0]['id']=='cert'
    assert evaluate(role,cv,evidence,['docker','docker'])['input_sha256']==updated['input_sha256']

@pytest.mark.parametrize('requirements',[[{'skill':'Docker','weight':0}],[{'skill':'Docker','weight':float('nan')}],[{'skill':'---'}],[{'skill':'Postgres'},{'skill':'PostgreSQL'}]])
def test_invalid_or_duplicate_requirements_rejected(requirements):
    with pytest.raises(ValueError):RoleDocument(title='Role',requirements=requirements)
