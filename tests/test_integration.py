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
    assert client.get('/health').json()['schema_version']==2

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
    prefs={'theme':'dark','reduced_motion':True,'reduced_transparency':True}
    assert client.patch('/api/me',headers=owner,json={'display_name':'Lifecycle Developer','discoverable':True,'preferences':prefs}).status_code==200
    assert client.get('/api/me',headers=owner).json()['profile']['preferences']==prefs
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
