"""Integration tests use an isolated schema on a local test PostgreSQL database.
Run with AUCTOR_TEST_LOCAL=1; no production endpoint is contacted.
"""
import asyncio, os, secrets
from urllib.parse import urlparse
import asyncpg
import pytest
from fastapi.testclient import TestClient
from app.config import settings
from app.main import app
from app.domain import CATALOG

@pytest.fixture(autouse=True)
def isolated_rate_limits():
    from app.platform import LIMITS
    LIMITS.clear()

@pytest.fixture(scope='module')
def client(tmp_path_factory):
    if os.getenv('AUCTOR_TEST_LOCAL')!='1': pytest.skip('Set AUCTOR_TEST_LOCAL=1 for an isolated local PostgreSQL schema')
    dsn=os.getenv('AUCTOR_TEST_DSN',settings.db_dsn)
    if urlparse(dsn).hostname not in ('localhost','127.0.0.1','::1'): pytest.fail('Tests refuse non-local PostgreSQL')
    previous=(settings.database_url,settings.db_schema,settings.reviewer_emails,settings.storage_path,settings.app_env)
    schema='auctor_test_'+secrets.token_hex(8)
    settings.database_url=dsn; settings.db_schema=schema; settings.reviewer_emails='reviewer@example.test'
    settings.storage_path=str(tmp_path_factory.mktemp('auctor-private-files')); settings.app_env='test'
    try:
        with TestClient(app) as client: yield client
    finally:
        async def cleanup():
            conn=await asyncpg.connect(dsn)
            try: await conn.execute('DROP SCHEMA '+schema+' CASCADE')
            finally: await conn.close()
        asyncio.run(cleanup())
        settings.database_url,settings.db_schema,settings.reviewer_emails,settings.storage_path,settings.app_env=previous

def account(client,email,handle):
    response=client.post('/api/auth/register',json={'email':email,'password':'integration-password-123','handle':handle,'display_name':handle})
    assert response.status_code==201,response.text
    return {'Authorization':'Bearer '+response.json()['token']}

def test_owned_insights_badge_details_certificate_issuer_and_durable_review_audit(client):
    owner=account(client,'insight-owner@example.test','insight-owner'); other=account(client,'insight-other@example.test','insight-other')
    reviewer=account(client,'insight-reviewer@example.test','insight-reviewer')
    async def grant():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE users SET role='reviewer' WHERE email='insight-reviewer@example.test'")
    client.portal.call(grant)
    cv={'skills':['Docker','PostgreSQL','Unknown Tool'],'projects':[{'name':'Insight API','tech_stack':['Docker']}],'experience':[],'profiles':{}}
    assert client.put('/api/cv',headers=owner,json={'data':cv}).status_code==200
    aid=client.post('/api/challenges/docker/attempts',headers=owner).json()['id']
    assert client.post('/api/attempts/'+aid+'/submit',headers=owner,json={'answers':{str(i):q[2] for i,q in enumerate(CATALOG['docker'][2])}}).status_code==200
    detail=client.get('/api/challenges/docker',headers=owner).json()
    assert detail['earned'] and len(detail['attempts'])==1 and 'answers' not in detail['attempts'][0]
    assert client.get('/api/challenges/docker',headers=other).json()['attempts']==[]
    certificate=client.post('/api/evidence',headers=owner,json={'kind':'certificate','title':'Synthetic certificate','url':'https://example.test/certificate','detail':{'issuer':'Synthetic issuer','reference':'QA-123','issued_on':'2026-10-01'}}).json()['id']
    assert client.post('/api/reviews/'+certificate,headers=reviewer,json={'status':'verified','note':'Inspected issuer/source for synthetic workflow testing'}).status_code==200
    state=client.get('/api/me',headers=owner).json()
    nodes={n['name']:n for n in state['insights']['skill_graph']['nodes']}
    assert nodes['Docker']['status']=='assessed' and nodes['PostgreSQL']['status']=='claimed'
    assert state['review_audit'][0]['source']['detail']['issuer']=='Synthetic issuer'
    assert client.patch('/api/me',headers=owner,json={'display_name':'Insight owner','discoverable':True}).status_code==200
    assert len(client.get('/api/candidates?q=Unknown%20Tool',headers=other).json())==1
    cv['skills']=['Docker','PostgreSQL']
    assert client.put('/api/cv',headers=owner,json={'data':cv}).status_code==200
    assert client.get('/api/candidates?q=Unknown%20Tool',headers=other).json()==[]
    assert any('pending → verified' in change for item in state['insights']['score_comparisons'] for change in item['changes'])
    assert client.get('/api/me',headers=other).json()['review_audit']==[]
    assert client.get('/api/reviews/audit',headers=other).status_code==403
    assert any(row['evidence_id']==certificate for row in client.get('/api/reviews/audit',headers=reviewer).json())
    assert client.delete('/api/evidence/'+certificate,headers=owner).status_code==200
    assert client.get('/api/me',headers=owner).json()['review_audit'][0]['evidence_id']==certificate
    assert client.get('/api/me',headers=owner).json()['score']['total']==.6

def test_profile_json_import_is_bounded_owned_and_honestly_unverified(client):
    import json
    owner=account(client,'import-owner@example.test','import-owner');other=account(client,'import-other@example.test','import-other')
    payload=json.dumps({'source_url':'https://leetcode.com/u/synthetic-local-qa','solved':150}).encode()
    result=client.post('/api/coding/import',headers=owner,files={'file':('profile.json',payload,'application/json')})
    assert result.status_code==201 and result.json()['status']=='pending'
    state=client.get('/api/me',headers=owner).json()
    assert state['score']['total']==0
    assert state['evidence'][0]['detail']['solved']==150 and 'not a provider-verified' in state['evidence'][0]['detail']['import_method']
    assert len(state['evidence'][0]['detail']['import_sha256'])==64
    assert client.get('/api/me',headers=other).json()['evidence']==[]
    assert client.post('/api/coding/import',headers=owner,files={'file':('bad.json',b'{','application/json')}).status_code==422
    assert client.post('/api/coding/import',headers=owner,files={'file':('large.json',b'x'*65537,'application/json')}).status_code==413
    assert client.post('/api/coding/import',headers=owner,files={'file':('source.json',json.dumps({'source_url':'https://private.example.test/profile','solved':10}).encode(),'application/json')}).status_code==422

def test_owned_workflows_and_idempotent_grading(client):
    one=account(client,'one@example.test','developer-one');two=account(client,'two@example.test','developer-two')
    assert client.get('/api/me').status_code==401
    assert client.get('/api/me?user_id=1',headers=two).json()['profile']['handle']=='developer-two'
    cv={'skills':['Docker'],'projects':[{'name':'API','description':'Service','tech_stack':['Docker']}],'experience':[],'profiles':{'email':'private@example.test'}}
    assert client.put('/api/cv',headers=one,json={'data':cv}).status_code==200
    assert client.get('/api/me',headers=two).json()['cv']['skills']==[]
    attempt=client.post('/api/challenges/docker/attempts',headers=one).json()
    assert all('correct' not in q for q in attempt['questions'])
    answers={str(i):q[2] for i,q in enumerate(CATALOG['docker'][2])}
    assert client.post('/api/attempts/'+attempt['id']+'/submit',headers=two,json={'answers':answers}).status_code==404
    result=client.post('/api/attempts/'+attempt['id']+'/submit',headers=one,json={'answers':answers})
    assert result.status_code==200,result.text
    assert result.json()['score_delta']==.6
    replay=client.post('/api/attempts/'+attempt['id']+'/submit',headers=one,json={'answers':answers}).json()
    assert replay['replayed'] and replay['score_delta']==.6
    assert client.get('/api/me',headers=one).json()['score']['total']==.6
    fresh=client.post('/api/challenges/docker/attempts',headers=one).json()
    wrong={str(i):(q[2]+1)%4 for i,q in enumerate(CATALOG['docker'][2])}
    failed=client.post('/api/attempts/'+fresh['id']+'/submit',headers=one,json={'answers':wrong}).json()
    assert failed['passed'] is False and failed['score_delta']==0
    assert client.get('/api/me',headers=one).json()['score']['total']==.6
    expired=client.post('/api/challenges/docker/attempts',headers=one).json()
    async def expire():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE attempts SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",expired['id'])
    client.portal.call(expire)
    assert client.post('/api/attempts/'+expired['id']+'/submit',headers=one,json={'answers':answers}).status_code==409
    share=client.post('/api/shares',headers=one).json()['id']
    public=client.get('/api/share/'+share).json()
    assert 'email' not in str(public)
    assert client.delete('/api/shares/'+share,headers=two).status_code==404
    assert client.delete('/api/shares/'+share,headers=one).status_code==200
    assert client.get('/api/share/'+share).status_code==404
    assert client.post('/api/cv/jobs',headers=one,files={'file':('bad.pdf',b'not pdf','application/pdf')}).status_code==400
    assert client.get('/api/score?user_id=1').status_code==404

def test_reviewer_and_discovery_privacy(client):
    dev=account(client,'proof@example.test','proof-developer');reviewer=account(client,'reviewer@example.test','independent-reviewer')
    assert client.get('/api/me',headers=reviewer).json()['profile']['role']=='developer'
    async def assign_reviewer():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE users SET role='reviewer' WHERE email='reviewer@example.test'")
    client.portal.call(assign_reviewer)
    submission=client.post('/api/evidence',headers=dev,json={'kind':'coding','title':'Coding profile','url':'https://example.test/profile','detail':{'solved':150}})
    assert submission.status_code==201,submission.text
    eid=submission.json()['id']
    assert client.get('/api/reviews',headers=dev).status_code==403
    assert client.post('/api/reviews/'+eid,headers=dev,json={'status':'verified','note':'Reviewed public proof source'}).status_code==403
    assert client.post('/api/reviews/'+eid,headers=reviewer,json={'status':'verified','note':'Reviewed source and confirmed count'}).status_code==200
    assert client.get('/api/me',headers=dev).json()['score']['total']==.75
    assert client.get('/api/public/proof-developer').status_code==404
    assert client.patch('/api/me',headers=dev,json={'display_name':'Proof Developer','bio':'API builder','discoverable':True,'preferences':{'theme':'dark'}}).status_code==200
    assert client.get('/api/public/proof-developer').status_code==200
    listed=client.get('/api/candidates?q=proof&min_score=0.5',headers=reviewer)
    assert listed.status_code==200,listed.text
    assert len(listed.json())==1
    assert client.get('/api/export?format=pdf',headers=dev).content.startswith(b'%PDF-')
    assert client.get('/api/badge/proof-developer.svg').status_code==200
    assert client.get('/health').json()['schema_version']==8

def test_oauth_ownership_and_project_binding(client,monkeypatch):
    import httpx
    from urllib.parse import parse_qs,urlparse
    from app import platform
    owner=account(client,'github@example.test','github-developer')
    monkeypatch.setattr(settings,'github_client_id','test-client');monkeypatch.setattr(settings,'github_client_secret','test-secret')
    cv={'skills':['REST API'],'projects':[{'name':'Owned API'}],'experience':[],'profiles':{}}
    client.put('/api/cv',headers=owner,json={'data':cv})
    start=client.post('/api/github/connect',headers=owner)
    state=parse_qs(urlparse(start.json()['url']).query)['state'][0]
    def provider(request):
        if request.url.path=='/login/oauth/access_token':return httpx.Response(200,json={'access_token':'isolated-test-token'})
        if request.url.path=='/user':return httpx.Response(200,json={'id':123,'login':'fixture-owner','html_url':'https://github.com/fixture-owner'})
        if request.url.path=='/user/repos':return httpx.Response(200,json=[{'name':'owned-api','full_name':'fixture-owner/owned-api','html_url':'https://github.com/fixture-owner/owned-api','private':False,'language':'Python','stargazers_count':2,'fork':False,'updated_at':'2026-10-01T00:00:00Z'}])
        return httpx.Response(200,json=[])
    original=httpx.AsyncClient
    monkeypatch.setattr(platform.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(provider),**kwargs))
    callback=client.get('/api/github/callback?state='+state+'&code=test-code',follow_redirects=False)
    assert callback.status_code==303,callback.text
    assert client.get('/api/github/callback?state='+state+'&code=test-code').status_code==400
    add=client.post('/api/evidence',headers=owner,json={'kind':'project','title':'Owned API source','url':'https://github.com/fixture-owner/owned-api','detail':{'repository':'fixture-owner/owned-api','project':'Owned API'}})
    assert add.status_code==201,add.text
    assert add.json()['status']=='verified'
    assert client.get('/api/me',headers=owner).json()['score']['total']==4
    assert client.delete('/api/github',headers=owner).status_code==200
    assert client.get('/api/me',headers=owner).json()['score']['total']==0


def test_persistent_cv_job_readback_and_source_ownership(client,monkeypatch):
    import io,time
    from reportlab.pdfgen import canvas
    monkeypatch.setattr(settings,'openai_api_key','')
    owner=account(client,'upload@example.test','cv-uploader');other=account(client,'reader@example.test','private-reader')
    buffer=io.BytesIO();pdf=canvas.Canvas(buffer);pdf.drawString(80,780,'Alex Developer');pdf.drawString(80,750,'Skills: Python, Docker, PostgreSQL');pdf.drawString(80,720,'Projects');pdf.drawString(80,690,'Orders API');pdf.drawString(80,660,'Built REST API using FastAPI and PostgreSQL');pdf.save()
    queued=client.post('/api/cv/jobs',headers=owner,files={'file':('resume.pdf',buffer.getvalue(),'application/pdf')})
    assert queued.status_code==202,queued.text
    jid=queued.json()['id']
    assert client.get('/api/cv/jobs/'+jid,headers=other).status_code==404
    assert client.get('/api/cv/jobs/'+jid+'/file',headers=other).status_code==404
    assert client.get('/api/cv/jobs/'+jid+'/file',headers=owner).content.startswith(b'%PDF-')
    status={}
    for _ in range(60):
        status=client.get('/api/cv/jobs/'+jid,headers=owner).json()
        if status['status'] in ('succeeded','failed'):break
        time.sleep(.1)
    assert status['status']=='succeeded',status
    saved=client.get('/api/me',headers=owner).json()
    assert 'Docker' in saved['cv']['skills']
    assert saved['documents'][0]['source']['verified'] is False
    assert saved['versions']


def test_revision_proof_review_recruiter_and_account_lifecycle(client):
    owner=account(client,'lifecycle@example.test','lifecycle-developer')
    reader=account(client,'lifecycle-reader@example.test','lifecycle-reader')
    reviewer=account(client,'lifecycle-reviewer@example.test','lifecycle-reviewer')
    cv={'skills':['Docker'],'projects':[],'experience':[],'profiles':{'email':'private-lifecycle@example.test'}}
    assert client.put('/api/cv',headers=owner,json={'data':cv}).status_code==200
    version=client.get('/api/me',headers=owner).json()['versions'][0]['id']
    assert client.put('/api/cv',headers=owner,json={'data':{**cv,'skills':['Redis']}}).status_code==200
    assert client.post(f'/api/cv/versions/{version}/restore',headers=reader).status_code==404
    assert client.post(f'/api/cv/versions/{version}/restore',headers=owner).status_code==200
    assert client.get('/api/me',headers=owner).json()['cv']['skills']==['Docker']
    assert len(client.get('/api/challenges',headers=owner).json())==5
    uid=client.get('/api/me',headers=owner).json()['profile']['id']
    async def role_and_running_job():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE users SET role='reviewer' WHERE email IN ('lifecycle@example.test','lifecycle-reviewer@example.test')")
            await c.execute("INSERT INTO documents(id,user_id,filename,storage_key,status,run_token) VALUES('lifecycle-job',$1,'test.pdf','test-only-missing.pdf','running','isolated-lease')",uid)
    client.portal.call(role_and_running_job)
    assert client.post('/api/cv/jobs/lifecycle-job/cancel',headers=reader).status_code==404
    assert client.post('/api/cv/jobs/lifecycle-job/cancel',headers=owner).status_code==200
    assert client.get('/api/cv/jobs/lifecycle-job',headers=owner).json()['status']=='cancelled'
    assert client.post('/api/cv/jobs/lifecycle-job/retry',headers=owner).status_code==200
    assert client.get('/api/cv/jobs/lifecycle-job',headers=owner).json()['status'] in ('queued','running','failed')
    proof=b'%PDF-1.4\n% isolated proof access fixture\n%%EOF'
    for kind in ('experience','certificate'):
        eid=client.post('/api/evidence',headers=owner,json={'kind':kind,'title':'Isolated '+kind}).json()['id']
        assert client.post('/api/reviews/'+eid,headers=owner,json={'status':'verified','note':'I reviewed my own evidence'}).status_code==403
        assert client.post('/api/evidence/'+eid+'/file',headers=reader,files={'file':('proof.pdf',proof,'application/pdf')}).status_code==404
        assert client.post('/api/evidence/'+eid+'/file',headers=owner,files={'file':('proof.pdf',proof,'application/pdf')}).status_code==200
        assert client.get('/api/evidence/'+eid+'/file',headers=reader).status_code==404
        assert client.get('/api/evidence/'+eid+'/file',headers=owner).content==proof
        assert client.get('/api/evidence/'+eid+'/file',headers=reviewer).content==proof
        assert client.post('/api/reviews/'+eid,headers=reviewer,json={'status':'verified','note':'Independently inspected this private proof'}).status_code==200
        assert client.post('/api/evidence/'+eid+'/file',headers=owner,files={'file':('changed.pdf',proof,'application/pdf')}).status_code==409
        assert client.delete('/api/evidence/'+eid,headers=reader).status_code==404
    assert client.get('/api/me',headers=owner).json()['score']['total']==1.5
    prefs={'theme':'dark','reduced_motion':True,'reduced_transparency':True,'high_contrast':True}
    assert client.patch('/api/me',headers=owner,json={'display_name':'Lifecycle Developer','discoverable':True,'preferences':prefs}).status_code==200
    assert client.get('/api/me',headers=owner).json()['profile']['preferences']==prefs
    assert client.patch('/api/me',headers=owner,json={'display_name':'Lifecycle Developer','preferences':{'high_contrast':'true'}}).status_code==422
    assert client.patch('/api/me',headers=owner,json={'display_name':'Lifecycle Developer','preferences':{'theme':'unknown'}}).status_code==422
    assert client.patch('/api/me',headers=owner,json={'display_name':'Lifecycle Developer','preferences':{'oversized':'x'*4097}}).status_code==422
    assert client.post(f'/api/candidates/{uid}/save',headers=reader).status_code==200
    assert client.get('/api/candidates?q=lifecycle-developer',headers=reader).json()[0]['saved'] is True
    assert client.get('/api/candidates?q=lifecycle-developer',headers=reviewer).json()[0]['saved'] is False
    assert client.delete(f'/api/candidates/{uid}/save',headers=reader).status_code==200
    assert client.get('/api/candidates?q=lifecycle-developer',headers=reader).json()[0]['saved'] is False
    export=client.get('/api/export?format=json',headers=owner).json()
    assert 'private-lifecycle@example.test' not in str(export)
    assert all('storage_key' not in e for e in export['evidence'])
    assert client.post('/api/activity/read',headers=owner).status_code==200
    workspace=client.get('/api/me',headers=owner).json()
    assert workspace['activity'] and all(a['read'] for a in workspace['activity'])
    assert len(workspace['history'])>=2
    for e in workspace['evidence']:
        assert client.delete('/api/evidence/'+e['id'],headers=owner).status_code==200
    assert client.get('/api/me',headers=owner).json()['score']['total']==0
    assert client.post('/api/auth/logout',headers=owner).status_code==200
    assert client.get('/api/me',headers=owner).status_code==401


def test_career_story_owned_revisions_public_redaction_and_reordering(client):
    owner=account(client,'career-owner@example.test','career-owner')
    other=account(client,'career-other@example.test','career-other')
    evidence=client.post('/api/evidence',headers=owner,json={'kind':'certificate','title':'Career source','detail':{'issuer':'Illustrative issuer'}}).json()['id']
    foreign=client.post('/api/evidence',headers=other,json={'kind':'certificate','title':'Private other source'}).json()['id']
    milestone={'id':'first','title':'Built a service','date':'2026-01-01','kind':'project','narrative':'Candidate description','annotation':'First release','visible':True,'evidence_ids':[evidence]}
    initial=client.get('/api/career-story',headers=owner).json();assert initial=={'revision':0,'milestones':[]}
    invalid=client.put('/api/career-story',headers=owner,json={'revision':0,'milestones':[{**milestone,'evidence_ids':[foreign]}]});assert invalid.status_code==404
    saved=client.put('/api/career-story',headers=owner,json={'revision':0,'milestones':[milestone,{**milestone,'id':'private','visible':False,'title':'Private milestone'}]});assert saved.status_code==200,saved.text
    assert client.get('/api/career-story',headers=other).json()['milestones']==[]
    assert client.put('/api/career-story',headers=owner,json={'revision':0,'milestones':[]}).status_code==409
    share=client.post('/api/shares',headers=owner).json()['id']
    public=client.get('/api/share/'+share).json()['career_story']['milestones']
    assert len(public)==1 and not public[0]['evidence'] and 'Candidate-written' in public[0]['narrative_status']
    async def verify():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE evidence SET status='verified' WHERE id=$1",evidence)
    client.portal.call(verify)
    public=client.get('/api/share/'+share).json()['career_story']['milestones'];assert public[0]['evidence'][0]['id']==evidence
    assert 'detail' not in public[0]['evidence'][0] and 'evidence_ids' not in public[0]
    reversed_items=list(reversed(saved.json()['milestones']))
    result=client.put('/api/career-story',headers=owner,json={'revision':1,'milestones':reversed_items});assert result.status_code==200
    assert client.get('/api/me',headers=owner).json()['career_story']['milestones'][0]['id']=='private'
    assert client.put('/api/career-story',headers=owner,json={'revision':2,'milestones':[milestone,milestone]}).status_code==422
    assert client.put('/api/career-story',headers=owner,json={'revision':2,'milestones':[{**milestone,'date':'2026-02-30'}]}).status_code==422
    client.delete('/api/shares/'+share,headers=owner);assert client.get('/api/share/'+share).status_code==404


def test_role_fit_review_weights_ownership_reproducibility_and_score_separation(client):
    owner=account(client,'role-owner@example.test','role-owner');other=account(client,'role-other@example.test','role-other')
    assert client.post('/api/role-fit/runs',headers=owner).status_code==400
    payload={'revision':0,'title':'Platform engineer','description':'Docker and PostgreSQL','reviewed':True,'requirements':[{'skill':'Docker','priority':'required'},{'skill':'PostgreSQL','priority':'preferred'}]}
    assert client.put('/api/role-fit',headers=owner,json=payload).status_code==200
    assert client.put('/api/role-fit',headers=owner,json=payload).status_code==409
    before=client.get('/api/me',headers=owner).json()['score']
    first=client.post('/api/role-fit/runs',headers=owner).json();second=client.post('/api/role-fit/runs',headers=owner).json()
    assert first['result']['input_sha256']==second['result']['input_sha256']
    assert first['result']['total_weight']==3 and first['result']['coverage_percent']==0
    assert all(r['status']=='unknown' for r in first['result']['contributions'])
    assert client.get('/api/role-fit',headers=other).json()['runs']==[]
    assert client.get('/api/me',headers=owner).json()['score']==before
    invalid={**payload,'revision':1,'requirements':[{'skill':'Postgres'},{'skill':'PostgreSQL'}]}
    assert client.put('/api/role-fit',headers=owner,json=invalid).status_code==422


def test_snapshot_comparison_annotations_pdf_and_owned_practice(client,monkeypatch):
    import time,io
    from reportlab.pdfgen.canvas import Canvas
    owner=account(client,'workbench-owner@example.test','workbench-owner');other=account(client,'workbench-other@example.test','workbench-other')
    from app.config import settings
    monkeypatch.setattr(settings,'openai_api_key','')
    for skill in ['Docker','PostgreSQL']:
        assert client.put('/api/cv',headers=owner,json={'data':{'skills':[skill],'projects':[],'experience':[],'profiles':{}}}).status_code==200
    versions=client.get('/api/me',headers=owner).json()['versions'];ids=[]
    def ready(sid):
        for _ in range(100):
            item=next(s for s in client.get('/api/workbench/snapshots',headers=owner).json() if s['id']==sid)
            if item['status'] in ('ready','failed','cancelled'):return item
            time.sleep(.05)
        raise AssertionError('Snapshot worker timed out')
    for version in versions:
        result=client.post('/api/workbench/snapshots',headers=owner,json={'kind':'cv_version','source_id':str(version['id'])});assert result.status_code==202,result.text
        sid=result.json()['id'];assert ready(sid)['status']=='ready';ids.append(sid)
    assert client.post('/api/workbench/snapshots',headers=other,json={'kind':'cv_version','source_id':str(versions[0]['id'])}).status_code==404
    assert client.get('/api/workbench/snapshots',headers=other).json()==[]
    comparison=client.post('/api/workbench/comparisons',headers=owner,json={'left_id':ids[0],'right_id':ids[1]});assert comparison.status_code==201,comparison.text
    cid=comparison.json()['id'];assert client.get('/api/workbench/comparisons/'+cid,headers=other).status_code==404
    assert client.get('/api/workbench/snapshots/'+ids[0],headers=other).status_code==404
    listed=client.get('/api/workbench/comparisons',headers=owner).json()[0];assert any(op[0]!='equal' for op in listed['data']['operations'])
    note={'revision':0,'annotations':[{'side':'left','line':1,'text':'Claim revision changed'}]}
    assert client.put('/api/workbench/comparisons/'+cid+'/annotations',headers=other,json=note).status_code==404
    assert client.put('/api/workbench/comparisons/'+cid+'/annotations',headers=owner,json=note).status_code==200
    assert client.put('/api/workbench/comparisons/'+cid+'/annotations',headers=owner,json=note).status_code==409
    assert client.put('/api/workbench/comparisons/'+cid+'/annotations',headers=owner,json={'revision':1,'annotations':[{'side':'right','line':1999,'text':'Out of range'}]}).status_code==422
    buffer=io.BytesIO();pdf=Canvas(buffer);pdf.drawString(60,750,'Original PDF text Docker 2026');pdf.save();pdfbytes=buffer.getvalue()
    upload=client.post('/api/cv/jobs',headers=owner,files={'file':('Comparison.pdf',pdfbytes,'application/pdf')});assert upload.status_code==202
    job=upload.json()['id']
    for _ in range(100):
        if client.get('/api/cv/jobs/'+job,headers=owner).json()['status']=='succeeded':break
        time.sleep(.05)
    version=client.get('/api/me',headers=owner).json()['versions'][0]
    pdfsnapshot=client.post('/api/workbench/snapshots',headers=owner,json={'kind':'cv_version','source_id':str(version['id'])}).json()['id']
    actual=ready(pdfsnapshot);assert actual['status']=='ready',actual
    assert 'Original PDF text Docker 2026' in actual['data']['text'] and 'file_sha256' in actual['data']
    assert client.get('/api/cv/jobs/'+job+'/file',headers=owner).content==pdfbytes
    assert client.get('/api/cv/jobs/'+job+'/file',headers=other).status_code==404
    evidence=client.post('/api/evidence',headers=owner,json={'kind':'certificate','title':'Practice context'}).json()['id']
    assert client.post('/api/workbench/practice',headers=owner,json={'role_revision':0,'requirement_indexes':[0]}).status_code==400
    role={'revision':0,'title':'Engineer','description':'Docker','reviewed':True,'requirements':[{'skill':'Docker'}]}
    assert client.put('/api/role-fit',headers=owner,json=role).status_code==200
    assert client.post('/api/workbench/practice',headers=other,json={'role_revision':0,'requirement_indexes':[0],'evidence_ids':[evidence]}).status_code==400
    practice=client.post('/api/workbench/practice',headers=owner,json={'role_revision':1,'requirement_indexes':[0],'evidence_ids':[evidence],'duration_seconds':60});assert practice.status_code==201,practice.text
    sid=practice.json()['id'];assert client.get('/api/workbench/practice/'+sid,headers=other).status_code==404
    assert client.post('/api/workbench/practice',headers=owner,json={'role_revision':0,'requirement_indexes':[0]}).status_code==409
    before=client.get('/api/me',headers=owner).json()['score']
    patch={'revision':0,'answers':{'0':'I reduced failures by 20 percent using measured container tests.'},'review':{'0':{'situation':True,'outcome':True}}}
    assert client.put('/api/workbench/practice/'+sid,headers=other,json=patch).status_code==404
    saved=client.put('/api/workbench/practice/'+sid,headers=owner,json=patch);assert saved.status_code==200,saved.text
    assert saved.json()['feedback']['items'][0]['has_numeric_detail'] is True
    assert client.put('/api/workbench/practice/'+sid,headers=owner,json=patch).status_code==409
    assert client.put('/api/workbench/practice/'+sid,headers=owner,json={**patch,'revision':1,'finish':True}).status_code==200
    assert client.put('/api/workbench/practice/'+sid,headers=owner,json={**patch,'revision':2}).status_code==409
    current=client.get('/api/workbench/practice',headers=owner).json()['sessions'][0]
    assert current['status']=='completed' and current['answers']['0']==patch['answers']['0'] and current['source']['evidence'][0]['id']==evidence
    assert client.get('/api/workbench/practice',headers=other).json()['sessions']==[]
    assert client.get('/api/me',headers=owner).json()['score']==before


def test_snapshot_cancel_retry_and_cached_repository_freeze(client,monkeypatch):
    import threading,time,json
    import app.workbench as workbench
    owner=account(client,'snapshot-state@example.test','snapshot-state')
    uid=client.get('/api/me',headers=owner).json()['profile']['id']
    async def repository(description):
        from app.platform import db
        async with db() as c: await c.execute('UPDATE users SET github_identity=$2::jsonb WHERE id=$1',uid,json.dumps({'repositories':[{'full_name':'qa/service','description':description}],'synced_at':'2026-10-15T00:00:00Z'}))
    client.portal.call(repository,'Original cached description')
    started=threading.Event();release=threading.Event();original=workbench.snapshot_text
    def controlled(source,content=None):
        started.set();release.wait(5);return original(source,content)
    monkeypatch.setattr(workbench,'snapshot_text',controlled)
    sid=client.post('/api/workbench/snapshots',headers=owner,json={'kind':'repository','source_id':'qa/service'}).json()['id']
    assert started.wait(3)
    assert client.post('/api/workbench/snapshots/'+sid+'/cancel',headers=owner).status_code==200
    release.set();time.sleep(.2)
    current=next(r for r in client.get('/api/workbench/snapshots',headers=owner).json() if r['id']==sid);assert current['status']=='cancelled'
    client.portal.call(repository,'Changed later cached description')
    assert client.post('/api/workbench/snapshots/'+sid+'/retry',headers=owner).status_code==200
    for _ in range(100):
        current=next(r for r in client.get('/api/workbench/snapshots',headers=owner).json() if r['id']==sid)
        if current['status']=='ready':break
        time.sleep(.05)
    assert current['status']=='ready' and 'Original cached description' in current['data']['text'] and 'Changed later' not in current['data']['text']


def test_recruiter_presentation_review_scope_expiry_revocation_and_pdf(client):
    import json,io
    from pdfminer.high_level import extract_text
    owner=account(client,'presentation-owner@example.test','presentation-owner');other=account(client,'presentation-other@example.test','presentation-other')
    cv={'skills':['Docker'],'projects':[{'name':'Selected service','description':'A real observable outcome','tech_stack':['Docker']}],'experience':[],'profiles':{'email':'private-contact@example.test'}}
    assert client.put('/api/cv',headers=owner,json={'data':cv}).status_code==200
    evidence=client.post('/api/evidence',headers=owner,json={'kind':'certificate','title':'Selected evidence','detail':{'skills':['Docker'],'private':'SECRET_PROOF'}}).json()['id']
    foreign=client.post('/api/evidence',headers=other,json={'kind':'certificate','title':'Other account'}).json()['id']
    async def verify():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE evidence SET status='verified' WHERE id=$1",evidence)
    client.portal.call(verify)
    assert client.put('/api/career-story',headers=owner,json={'revision':0,'milestones':[{'id':'public','date':'2026-01-01','title':'Public milestone','narrative':'Released a service','visible':True,'evidence_ids':[evidence]},{'id':'private','date':'2026-01-02','title':'SECRET_MILESTONE','visible':False}]}).status_code==200
    role={'revision':0,'title':'PRIVATE_ROLE_TITLE','description':'PRIVATE_JOB_SPEC','reviewed':True,'requirements':[{'skill':'Docker'}]}
    assert client.put('/api/role-fit',headers=owner,json=role).status_code==200
    run=client.post('/api/role-fit/runs',headers=owner).json()['id']
    body={'revision':0,'title':'Selected recruiter story','introduction':'A focused candidate introduction','private_notes':'SECRET_NOTES','milestone_ids':['public','private'],'project_names':['Selected service'],'evidence_ids':[evidence],'role_run_id':run}
    assert client.put('/api/presentations',headers=owner,json={**body,'evidence_ids':[foreign]}).status_code==404
    assert client.put('/api/presentations',headers=owner,json=body).status_code==200
    assert client.put('/api/presentations',headers=owner,json=body).status_code==409
    preview=client.get('/api/presentations/preview',headers=owner).json()
    public=json.dumps(preview['data']);assert all(secret not in public for secret in ['SECRET_NOTES','SECRET_PROOF','SECRET_MILESTONE','private-contact','PRIVATE_JOB_SPEC','PRIVATE_ROLE_TITLE'])
    assert len(preview['data']['career_story']['milestones'])==1 and preview['data']['role_fit']['contributions'][0]['status']=='publicly evidenced'
    publish={'revision':1,'preview_sha256':preview['preview_sha256'],'expires_days':7}
    assert client.post('/api/presentations/shares',headers=owner,json=publish).status_code==400
    assert client.post('/api/presentations/shares',headers=other,json={**publish,'confirm_public':True}).status_code==409
    published=client.post('/api/presentations/shares',headers=owner,json={**publish,'confirm_public':True});assert published.status_code==201,published.text
    sid=published.json()['id'];assert client.get('/api/share/'+sid).status_code==404
    assert client.get('/api/presentations/shared/'+sid).status_code==200
    pdf=client.get('/api/presentations/shared/'+sid+'/brief.pdf');assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')
    text=extract_text(io.BytesIO(pdf.content));assert 'Selected recruiter story' in text and 'Selected service' in text and 'SECRET' not in text
    output=__import__('pathlib').Path('../Auctor-F-end-Flutter/output/pdf');output.mkdir(parents=True,exist_ok=True);(output/'auctor-interview-brief.pdf').write_bytes(pdf.content)
    async def revoke_source():
        from app.platform import db
        async with db() as c:await c.execute("UPDATE evidence SET status='rejected' WHERE id=$1",evidence)
    client.portal.call(revoke_source)
    data=client.get('/api/presentations/shared/'+sid).json();assert data['evidence']==[] and data['role_fit']['contributions'][0]['sources']==[]
    assert client.post('/api/presentations/shares',headers=owner,json={**publish,'confirm_public':True}).status_code==409
    assert client.delete('/api/shares/'+sid,headers=owner).status_code==200
    assert client.get('/api/presentations/shared/'+sid+'/brief.pdf').status_code==404
    fresh=client.get('/api/presentations/preview',headers=owner).json();new=client.post('/api/presentations/shares',headers=owner,json={'revision':1,'preview_sha256':fresh['preview_sha256'],'confirm_public':True,'expires_days':1}).json()['id']
    async def expire():
        from app.platform import db
        async with db() as c:await c.execute("UPDATE shares SET expires_at=NOW()-INTERVAL '1 second' WHERE id=$1",new)
    client.portal.call(expire);assert client.get('/api/presentations/shared/'+new).status_code==404


def test_receipt_rotation_tamper_reverification_and_human_integrity(client):
    from datetime import datetime,timezone,timedelta
    from app import verification_keys
    owner=account(client,'trust-owner@example.test','trust-owner');other=account(client,'trust-other@example.test','trust-other');reviewer=account(client,'trust-reviewer@example.test','trust-reviewer')
    async def grant():
        from app.platform import db
        async with db() as c:await c.execute("UPDATE users SET role='reviewer' WHERE email='trust-reviewer@example.test'")
    client.portal.call(grant)
    eid=client.post('/api/evidence',headers=owner,json={'kind':'certificate','title':'Scope test','url':'https://example.test/proof','detail':{'skills':['Docker'],'private':'PRIVATE_SOURCE'}}).json()['id']
    assert client.post('/api/verification/evidence/'+eid+'/receipts',headers=owner).status_code==409
    assert client.post('/api/reviews/'+eid,headers=reviewer,json={'status':'verified','note':'Inspected source for this controlled fixture'}).status_code==200
    issued=client.post('/api/verification/evidence/'+eid+'/receipts',headers=owner);assert issued.status_code==201,issued.text
    signed=issued.json();rid=signed['payload']['receipt_id'];assert 'PRIVATE_SOURCE' not in str(signed)
    status=client.post('/api/verification/verify',json=signed).json();assert status['signature_valid'] and status['currently_eligible'] and status['snapshot_current']
    tampered={**signed,'payload':{**signed['payload'],'scope':'Forged scope'}};assert not client.post('/api/verification/verify',json=tampered).json()['signature_valid']
    old=signed['payload']['key_id'];verification_keys.rotate();assert old in {k['key_id'] for k in client.get('/api/verification/keys').json()['keys']}
    assert client.post('/api/verification/verify',json=signed).json()['signature_valid']
    assert client.delete('/api/verification/receipts/'+rid,headers=other).status_code==404
    assert client.put('/api/verification/evidence/'+eid+'/lifecycle',headers=owner,json={'state':'stale','note':'Not a reviewer'}).status_code==403
    future=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
    assert client.put('/api/verification/evidence/'+eid+'/lifecycle',headers=reviewer,json={'state':'current','review_due_at':future,'note':'Assigned explicit review date'}).status_code==200
    async def due():
        from app.platform import db
        async with db() as c:await c.execute("UPDATE evidence SET review_due_at=NOW()-INTERVAL '1 second' WHERE id=$1",eid)
        from app.verification import expire_due
        await expire_due();await expire_due()
    client.portal.call(due)
    current=client.get('/api/verification/receipts/'+rid).json()['status'];assert current['signature_valid'] and not current['currently_eligible']
    checks=client.get('/api/verification/rechecks',headers=owner).json();assert len(checks)==1
    assert client.post('/api/verification/evidence/'+eid+'/recheck',headers=owner).json()['replayed']
    assert client.get('/api/verification/rechecks',headers=other).json()==[]
    assert client.post('/api/verification/rechecks/'+checks[0]['id']+'/decision',headers=reviewer,json={'approved':True,'state':'current','review_due_at':future,'note':'Manually inspected unchanged proof'}).status_code==200
    assert client.get('/api/verification/receipts/'+rid).json()['status']['currently_eligible']
    assert client.delete('/api/verification/receipts/'+rid,headers=owner).status_code==200
    revoked=client.get('/api/verification/receipts/'+rid).json()['status'];assert revoked['signature_valid'] and revoked['revoked']
    attempt=client.post('/api/challenges/docker/attempts',headers=owner).json()['id'];answers={'answers':{str(i):q[2] for i,q in enumerate(CATALOG['docker'][2])}}
    first=client.post('/api/attempts/'+attempt+'/submit',headers=owner,json=answers);assert first.status_code==200
    for _ in range(3):assert client.post('/api/attempts/'+attempt+'/submit',headers=owner,json=answers).json()['replayed']
    signals=client.get('/api/verification/integrity',headers=owner).json();assert {'rapid_submission','repeated_replays'}<={s['kind'] for s in signals}
    before=client.get('/api/me',headers=owner).json()['score']
    sid=signals[0]['id'];body={'disposition':'dismissed','note':'Controlled fixture, no adverse inference'}
    assert client.post('/api/verification/integrity/'+sid+'/review',headers=other,json=body).status_code==403
    assert client.post('/api/verification/integrity/'+sid+'/review',headers=reviewer,json=body).status_code==200
    assert client.get('/api/verification/integrity-audit',headers=owner).json()[-1]['disposition']=='dismissed'
    assert client.get('/api/me',headers=owner).json()['score']==before


def test_scoped_integrations_signed_local_delivery_and_revocation(client,monkeypatch):
    import threading,time,json,hmac,hashlib
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from app.config import settings
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            content=self.rfile.read(int(self.headers['Content-Length']));received.append((dict(self.headers),content));self.send_response(503 if len(received)==1 else 204);self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Receiver);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        owner=account(client,'integration-owner@example.test','integration-owner');other=account(client,'integration-other@example.test','integration-other')
        body={'name':'Local consent fixture','scopes':['profile:read','verification:read'],'webhook_url':f'http://127.0.0.1:{server.server_port}/updates','confirm_consent':True}
        assert client.post('/api/integrations',headers=owner,json=body).status_code==422
        monkeypatch.setattr(settings,'webhook_development_mode',True)
        created=client.post('/api/integrations',headers=owner,json=body);assert created.status_code==201,created.text
        value=created.json();iid=value['id'];token={'Authorization':'Bearer '+value['credential']}
        listing=client.get('/api/integrations',headers=owner).json();assert 'credential' not in str(listing) and 'secret_cipher' not in str(listing)
        assert client.get('/api/integrations/recruiter/profile',headers=token).status_code==200
        assert client.get('/api/integrations/recruiter/verification',headers=token).status_code==200
        assert client.get('/api/integrations/'+iid+'/deliveries',headers=other).status_code==404
        assert client.put('/api/cv',headers=owner,json={'data':{'skills':['Docker'],'projects':[],'experience':[],'profiles':{'email':'PRIVATE_CONTACT@example.test'}}}).status_code==200
        for _ in range(200):
            if len(received)>=2:break
            time.sleep(.05)
        assert len(received)>=2 and received[0][0]['X-Auctor-Event-Id']==received[1][0]['X-Auctor-Event-Id']
        headers,content=received[-1];expected=hmac.new(value['webhook_secret'].encode(),headers['X-Auctor-Timestamp'].encode()+b'.'+content,hashlib.sha256).hexdigest()
        assert headers['X-Auctor-Signature']=='v1='+expected and json.loads(content)['event_id']==headers['X-Auctor-Event-Id']
        assert b'PRIVATE_CONTACT' not in content and b'PRIVATE_CONTACT' not in client.get('/api/integrations/recruiter/profile',headers=token).content
        assert client.delete('/api/integrations/'+iid,headers=other).status_code==404
        assert client.delete('/api/integrations/'+iid,headers=owner).status_code==200
        assert client.get('/api/integrations/recruiter/profile',headers=token).status_code==401
        previous=len(received);client.put('/api/cv',headers=owner,json={'data':{'skills':['Redis'],'projects':[],'experience':[],'profiles':{}}});time.sleep(.3);assert len(received)==previous
        read_only=client.post('/api/integrations',headers=owner,json={'name':'Profile only','scopes':['profile:read'],'confirm_consent':True}).json()
        assert client.get('/api/integrations/recruiter/verification',headers={'Authorization':'Bearer '+read_only['credential']}).status_code==403
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
