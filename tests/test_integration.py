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

@pytest.fixture(scope='module')
def client():
    if os.getenv('AUCTOR_TEST_LOCAL')!='1': pytest.skip('Set AUCTOR_TEST_LOCAL=1 for an isolated local PostgreSQL schema')
    dsn=os.getenv('AUCTOR_TEST_DSN',settings.db_dsn)
    if urlparse(dsn).hostname not in ('localhost','127.0.0.1','::1'): pytest.fail('Tests refuse non-local PostgreSQL')
    previous=(settings.database_url,settings.db_schema,settings.reviewer_emails)
    schema='auctor_test_'+secrets.token_hex(8)
    settings.database_url=dsn; settings.db_schema=schema; settings.reviewer_emails='reviewer@example.test'
    try:
        with TestClient(app) as client: yield client
    finally:
        async def cleanup():
            conn=await asyncpg.connect(dsn)
            try: await conn.execute('DROP SCHEMA '+schema+' CASCADE')
            finally: await conn.close()
        asyncio.run(cleanup())
        settings.database_url,settings.db_schema,settings.reviewer_emails=previous

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
