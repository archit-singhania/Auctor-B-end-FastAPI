from datetime import datetime, timezone

from app.domain import score_from
from app.insights import build_insights, score_comparisons, signal_snapshot


def test_graph_links_claims_owned_sources_and_scoped_assessments_without_fabrication():
    cv={'skills':['Docker','PostgreSQL','Unknown Tool'], 'projects':[{'name':'API','tech_stack':['Docker','Redis']}]}
    evidence=[{'id':'owned','kind':'project','title':'Owned API','status':'verified','detail':{'project':'API'}}]
    insight=build_insights(cv,evidence,[{'badge_id':'docker','passed':True}],{},[])
    nodes={n['name']:n for n in insight['skill_graph']['nodes']}
    assert nodes['Docker']['status']=='assessed'
    assert {s['kind'] for s in nodes['Docker']['sources']}=={'cv','project','assessment'}
    assert nodes['Redis']['status']=='owned provenance'
    assert nodes['PostgreSQL']['status']=='claimed'
    assert any(edge['from']==nodes['Redis']['id'] and edge['to']=='project:API' for edge in insight['skill_graph']['edges'])
    roadmap={step['skill']:step for step in insight['roadmap']}
    assert roadmap['PostgreSQL']['track_id']=='postgres' and roadmap['PostgreSQL']['priority']==1
    assert roadmap['Unknown Tool']['track_id'] is None and 'No implemented' in roadmap['Unknown Tool']['reason']
    assert roadmap['Docker']['priority']==3


def test_cached_analytics_exposes_age_scope_and_languages_without_provider_calls():
    github={'synced_at':'2026-10-01T00:00:00+00:00','repositories':[{'language':'Python','stars':4},{'language':'Python','stars':2}], 'recent_activity':[{'type':'PushEvent'}]}
    analytics=build_insights({},[],[],github,[],now=datetime(2026,10,9,tzinfo=timezone.utc))['repository_analytics']
    assert analytics['freshness']=='stale' and analytics['age_seconds']==8*86400
    assert analytics['stars']==6 and analytics['languages']=={'Python':2}
    assert analytics['event_types']=={'PushEvent':1} and 'Not complete' in analytics['scope']


def test_score_comparison_records_zero_point_evidence_changes_and_legacy_baselines():
    empty={'skills':[], 'projects':[]}
    pending=[{'id':'certificate','kind':'certificate','title':'Fixture certificate','status':'pending','detail':{'issuer':'Synthetic issuer'}}]
    verified=[dict(pending[0],status='verified')]
    history=[{'id':1,'score':score_from([],[],[]),'signals':signal_snapshot(empty,[],[])},
             {'id':2,'score':score_from(pending,[],[]),'signals':signal_snapshot(empty,pending,[])},
             {'id':3,'score':score_from(verified,[],[]),'signals':signal_snapshot(empty,verified,[])}]
    changes=score_comparisons(history)
    assert changes[0]['total_delta'] is None
    assert changes[2]['total_delta']==0 and changes[2]['baseline_known']
    assert 'pending → verified' in changes[2]['changes'][0]
    assert score_comparisons([dict(history[0],signals={}),history[1]])[1]['baseline_known'] is False
