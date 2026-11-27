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


def test_concurrent_profile_lineage_search_and_exports_preserve_ownership(client):
    from concurrent.futures import ThreadPoolExecutor
    from platform import platform, python_version
    from statistics import median
    from time import perf_counter

    handles = [f'concurrent-reader-{index}' for index in range(8)]
    sessions = [account(client, f'{handle}@example.test', handle) for handle in handles]
    sources = []
    for handle, session in zip(handles, sessions):
        created = client.post('/api/evidence', headers=session, json={
            'kind': 'certificate', 'title': handle, 'detail': {},
            'url': 'https://example.test/fixture',
        })
        assert created.status_code == 201, created.text
        sources.append(created.json()['id'])
        role = client.put('/api/role-fit', headers=session, json={
            'revision': 0, 'title': handle, 'description': 'Docker deployment',
            'reviewed': True, 'requirements': [{'skill': 'Docker', 'priority': 'required'}],
        })
        assert role.status_code == 200, role.text

    def exercise(owner):
        timings = []
        requests = {name: [] for name in ('write', 'read', 'lineage', 'search', 'export', 'role fit')}
        role_hash = None

        def measured(name, method, route, **options):
            started = perf_counter()
            response = method(route, **options)
            requests[name].append((perf_counter() - started) * 1000)
            return response

        for revision in range(20):
            started = perf_counter()
            bio = f'Account {owner}, update {revision}'
            response = measured('write', client.patch, '/api/me', headers=sessions[owner], json={
                'display_name': handles[owner], 'bio': bio, 'discoverable': True,
            })
            assert response.status_code == 200, response.text
            response = measured('read', client.get, '/api/me', headers=sessions[owner])
            assert response.status_code == 200, response.text
            profile = response.json()['profile']
            assert profile['handle'] == handles[owner] and profile['bio'] == bio
            lineage = measured('lineage', client.get, f'/api/lineage/{sources[owner]}', headers=sessions[owner])
            assert lineage.status_code == 200, lineage.text
            assert lineage.json()['revisions'][0]['payload']['title'] == handles[owner]
            matches = measured('search', client.get, '/api/candidates', params={'q': handles[owner]},
                                 headers=sessions[owner])
            assert matches.status_code == 200, matches.text
            assert [(item['profile']['handle'], item['evidence']) for item in matches.json()] == [
                (handles[owner], []),
            ]
            exported = measured('export', client.get, '/api/export?format=json', headers=sessions[owner])
            assert exported.status_code == 200, exported.text
            assert exported.json()['profile']['handle'] == handles[owner]
            assert exported.json()['evidence'] == []
            fit = measured('role fit', client.post, '/api/role-fit/runs', headers=sessions[owner])
            assert fit.status_code == 201, fit.text
            result = fit.json()['result']
            assert result['total_weight'] == 2
            assert result['coverage_percent'] == 0
            if role_hash is None:
                role_hash = result['input_sha256']
            assert result['input_sha256'] == role_hash
            timings.append((perf_counter() - started) * 1000)
        foreign = client.get(f'/api/lineage/{sources[(owner + 1) % len(sources)]}',
                             headers=sessions[owner])
        assert foreign.status_code == 404
        pdf = client.get('/api/export?format=pdf', headers=sessions[owner])
        assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF-')
        return timings, requests

    workload_started = perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        samples = list(pool.map(exercise, range(8)))
    workload_seconds = perf_counter() - workload_started

    def report(label, values):
        values = sorted(values)
        p95 = values[(95 * len(values) + 99) // 100 - 1]
        print(f'{label} ({len(values)} samples): p50={median(values):.1f} ms, p95={p95:.1f} ms')

    cycles = [ms for durations, _ in samples for ms in durations]
    # Include the ownership and PDF checks that complete each account's run.
    request_count = len(cycles) * len(samples[0][1]) + len(samples) * 2
    print(f'{platform()}, Python {python_version()}, {os.cpu_count()} logical CPUs')
    print(f'{request_count} requests in {workload_seconds:.2f} s ({request_count / workload_seconds:.1f} requests/s)')
    report('Mixed six-request cycles across 8 owners', cycles)
    for operation in samples[0][1]:
        report(operation, [ms for _, requests in samples for ms in requests[operation]])


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
    assert client.get('/health').json()['schema_version']==19

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
    public = client.get('/api/presentations/shared/'+sid)
    assert public.status_code == 200
    assert '_source_bindings' not in public.json()
    lineage = client.get('/api/lineage/'+evidence, headers=owner).json()
    refs = [item for item in lineage['downstream'] if item['kind'] == 'recruiter_presentation']
    assert refs[0]['id'] == sid
    assert refs[0]['source_revision_ids'] == [lineage['revisions'][-1]['id']]
    assert client.get('/api/lineage/'+evidence, headers=other).status_code == 404
    pdf=client.get('/api/presentations/shared/'+sid+'/brief.pdf');assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')
    text=extract_text(io.BytesIO(pdf.content));assert 'Selected recruiter story' in text and 'Selected service' in text and 'SECRET' not in text
    async def revoke_source():
        from app.platform import db
        async with db() as c:await c.execute("UPDATE evidence SET status='rejected' WHERE id=$1",evidence)
    client.portal.call(revoke_source)
    data=client.get('/api/presentations/shared/'+sid).json();assert data['evidence']==[] and data['role_fit']['contributions'][0]['sources']==[]
    assert client.post('/api/presentations/shares',headers=owner,json={**publish,'confirm_public':True}).status_code==409
    assert client.delete('/api/shares/'+sid,headers=owner).status_code==200
    history = client.get('/api/lineage/'+evidence, headers=owner).json()
    historic = next(item for item in history['downstream'] if item['id'] == sid)
    assert historic['revoked']
    assert historic['source_revision_ids'] == refs[0]['source_revision_ids']
    async def simulate_legacy_share():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE recruiter_presentation_shares SET data=data-'_source_bindings' WHERE share_id=$1", sid)
    client.portal.call(simulate_legacy_share)
    legacy = client.get('/api/lineage/'+evidence, headers=owner).json()
    older = next(item for item in legacy['downstream'] if item['id'] == sid)
    assert older['source_revision_ids'] == []
    assert 'exact source revision unavailable' in older['binding']
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

def test_evidence_collection_owned_persistent_and_never_auto_verified(client):
    owner = account(client, 'collection-owner@example.test', 'collection-owner')
    other = account(client, 'collection-other@example.test', 'collection-other')
    cv = {'skills': ['Python'], 'projects': [], 'experience': [], 'profiles': {}}
    assert client.put('/api/cv', headers=owner, json={'data': cv}).status_code == 200
    path = '/api/evidence-collection'
    assert client.get(path).status_code == 401
    claim = client.get(path, headers=owner).json()['claims'][0]
    assert claim['status'] == 'missing source'
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Python course', 'url': 'https://example.test/course', 'detail': {}}).json()['id']
    link = path + '/' + claim['id'] + '/links'
    for _ in range(2):
        assert client.post(link, headers=owner, json={'evidence_id': source}).status_code == 200
    saved = client.get(path, headers=owner).json()['claims'][0]
    assert len(saved['linked']) == 1
    assert saved['status'] == 'no eligible reviewed source'
    assert client.get(path, headers=other).json()['available_evidence'] == []
    assert client.put('/api/cv', headers=other, json={'data': cv}).status_code == 200
    assert client.post(link, headers=other, json={'evidence_id': source}).status_code == 404
    cv['skills'] = ['Java']
    assert client.put('/api/cv', headers=owner, json={'data': cv}).status_code == 200
    assert client.post(link, headers=owner, json={'evidence_id': source}).status_code == 409
    assert client.get(path, headers=owner).json()['claims'][0]['linked'] == []
    lineage = client.get('/api/lineage/'+source, headers=owner).json()
    linked = next(item for item in lineage['downstream'] if item['kind'] == 'candidate_claim_link')
    assert linked['id'] == claim['id']
    assert [item['title'] for item in linked['matching_cv_revisions']] == ['Python']
    assert linked['source_revision_ids'] == [lineage['revisions'][0]['id']]
    assert linked['attachment']['claim_title'] == 'Python'
    assert client.get('/api/lineage/'+source, headers=other).status_code == 404
    async def withdraw_source():
        from app.platform import db
        async with db() as c:
            await c.execute('DELETE FROM evidence WHERE id=$1', source)
    client.portal.call(withdraw_source)
    history = client.get('/api/lineage/'+source, headers=owner).json()
    assert history['withdrawn']
    assert next(item for item in history['downstream']
                if item['kind'] == 'candidate_claim_link')['source_revision_ids'] == linked['source_revision_ids']

def test_private_applications_snapshot_revisions_and_calendar(client):
    import uuid
    owner = account(client, 'application-owner@example.test', 'application-owner')
    other = account(client, 'application-other@example.test', 'application-other')
    cv = {'skills': ['Python'], 'projects': [], 'experience': [], 'profiles': {}}
    assert client.put('/api/cv', headers=owner, json={'data': cv}).status_code == 200
    evidence = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Course', 'url': 'https://example.test/course', 'detail': {}}).json()['id']
    data = {'id': str(uuid.uuid4()), 'revision': 0, 'employer': 'Example', 'role': 'Developer', 'stage': 'saved', 'evidence_ids': [evidence], 'interviews': [{'title': 'Technical interview', 'starts_at': '2026-12-01T10:00:00+05:30', 'duration_minutes': 45}]}
    endpoint = '/api/applications'
    assert client.put(endpoint, headers=other, json=data).status_code == 404
    saved = client.put(endpoint, headers=owner, json=data)
    assert saved.status_code == 200, saved.text
    assert client.put(endpoint, headers=owner, json=data).json()['revision'] == 1
    data = saved.json()
    data['stage'] = 'applied'
    updated = client.put(endpoint, headers=owner, json=data).json()
    assert updated['revision'] == 2
    data['stage'] = 'offer'
    assert client.put(endpoint, headers=owner, json=data).status_code == 409
    snapshot_path = endpoint + '/' + data['id'] + '/snapshots'
    assert client.post(snapshot_path, headers=owner, json={'revision': 1}).status_code == 409
    first = client.post(snapshot_path, headers=owner, json={'revision': 2}).json()
    assert first['data']['cv']['skills'] == ['Python']
    assert first['data']['evidence'][0]['status'] == 'pending'
    source = first['data']['evidence'][0]['id']
    trace = client.get('/api/lineage/'+source, headers=owner)
    assert trace.status_code == 200, trace.text
    reference = next(item for item in trace.json()['downstream'] if item['kind'] == 'application_snapshot')
    assert reference['binding'] == 'Exact stored submission source revision'
    assert len(reference['source_revision_ids']) == 1
    assert client.get('/api/lineage/'+source, headers=other).status_code == 404
    cv['skills'] = ['Java']
    client.put('/api/cv', headers=owner, json={'data': cv})
    assert client.post(snapshot_path, headers=owner, json={'revision': 2}).json() == first
    assert client.get(snapshot_path, headers=other).status_code == 404
    assert client.get(endpoint, headers=other).json() == []
    assert client.get(endpoint, headers=owner).json()[0]['stage'] == 'applied'
    calendar = client.get(endpoint + '/' + data['id'] + '/calendar', headers=owner)
    assert calendar.status_code == 200
    assert 'DTSTART:20261201T043000Z' in calendar.text
    assert 'DTEND:20261201T051500Z' in calendar.text
    assert 'ATTENDEE' not in calendar.text
    assert client.get(endpoint + '/' + data['id'] + '/calendar', headers=other).status_code == 404
    updated['interviews'][0]['starts_at'] = '2026-12-01T10:00:00'
    assert client.put(endpoint, headers=owner, json=updated).status_code == 422
    withdrawn = client.delete('/api/evidence/'+source, headers=owner)
    assert withdrawn.status_code == 200, withdrawn.text
    historical = client.get('/api/lineage/'+source, headers=owner).json()
    assert historical['withdrawn'] and not historical['currently_eligible']
    archived_reference = next(item for item in historical['downstream'] if item['kind'] == 'application_snapshot')
    assert archived_reference == reference

def test_case_study_publication_redaction_snapshot_and_revocation(client):
    owner = account(client, 'case-owner@example.test', 'case-owner')
    other = account(client, 'case-other@example.test', 'case-other')
    endpoint = '/api/case-studies'
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Public course', 'url': 'https://example.test/private', 'detail': {'private': 'SECRET_DETAIL'}}).json()['id']
    draft = client.get(endpoint, headers=owner).json()
    draft['title'] = 'A reliable service'
    draft['sections'][0]['text'] = '<script>alert(1)</script> ORIGINAL'
    draft['sections'][0]['evidence_ids'] = [source]
    assert client.put(endpoint, headers=other, json=draft).status_code == 404
    saved = client.put(endpoint, headers=owner, json=draft).json()
    assert client.put(endpoint, headers=owner, json=draft).status_code == 409
    preview = client.get(endpoint + '/preview', headers=owner).json()
    assert preview['data']['sections'][0]['evidence'] == []
    async def verify():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE evidence SET status='verified' WHERE id=$1", source)
    client.portal.call(verify)
    body = {'revision': saved['revision'], 'sha256': preview['sha256'], 'confirm_public': True}
    assert client.post(endpoint + '/shares', headers=owner, json=body).status_code == 409
    preview = client.get(endpoint + '/preview', headers=owner).json()
    body['sha256'] = preview['sha256']
    published = client.post(endpoint + '/shares', headers=owner, json=body)
    assert published.status_code == 201, published.text
    sid = published.json()['id']
    path = endpoint + '/shared/' + sid
    public = client.get(path)
    assert public.status_code == 200
    assert '&lt;script&gt;' in public.text and '<script>' not in public.text
    assert 'SECRET_DETAIL' not in public.text and 'https://example.test/private' not in public.text
    assert 'Public course' in public.text
    saved['sections'][0]['text'] = 'DRAFT_EDIT'
    assert client.put(endpoint, headers=owner, json=saved).status_code == 200
    assert 'ORIGINAL' in client.get(path).text and 'DRAFT_EDIT' not in client.get(path).text
    pdf = client.get(path + '/report.pdf')
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF-')
    async def revoke_source():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE evidence SET eligibility_state='revoked' WHERE id=$1", source)
    client.portal.call(revoke_source)
    assert 'Public course' not in client.get(path).text
    assert 'historical sources' in client.get(path).text
    assert client.get(endpoint + '/shares', headers=other).json() == []
    assert client.delete('/api/shares/' + sid, headers=other).status_code == 404
    assert client.delete('/api/shares/' + sid, headers=owner).status_code == 200
    assert client.get(path).status_code == 404
    assert client.get(path + '/report.pdf').status_code == 404

def test_opt_in_analytics_privacy_bursts_owner_and_revocation(client):
    owner = account(client, 'analytics-owner@example.test', 'analytics-owner')
    other = account(client, 'analytics-other@example.test', 'analytics-other')
    endpoint = '/api/analytics'
    event = {'context': 'public', 'identifier': 'analytics-owner', 'kind': 'profile_view'}
    assert client.get(endpoint, headers=owner).json()['enabled'] is False
    assert client.post(endpoint + '/events', json=event).status_code == 404
    assert client.patch('/api/me', headers=owner, json={'display_name': 'Owner', 'discoverable': True}).status_code == 200
    assert client.post(endpoint + '/events', json=event).json()['counted'] is False
    assert client.put(endpoint, headers=owner, json={'enabled': True}).status_code == 200
    assert client.post(endpoint + '/events', headers=owner, json=event).json()['counted'] is False
    assert client.post(endpoint + '/events', json=event).json()['counted'] is True
    assert client.post(endpoint + '/events', headers=other, json=event).json()['counted'] is False
    assert client.get(endpoint, headers=owner).json()['days'][0]['count'] == 1
    assert client.get(endpoint, headers=other).json()['days'] == []
    share = client.post('/api/shares', headers=owner).json()['id']
    event['context'] = 'share'; event['identifier'] = share
    assert client.post(endpoint + '/events', json=event).json()['counted'] is True
    assert {d['kind'] for d in client.get(endpoint, headers=owner).json()['days']} == {'profile_view', 'share_activity'}
    client.delete('/api/shares/' + share, headers=owner)
    assert client.post(endpoint + '/events', json=event).status_code == 404
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Course', 'url': 'https://example.test/course', 'detail': {}}).json()['id']
    event = {'context': 'public', 'identifier': 'analytics-owner', 'kind': 'evidence_open', 'evidence_id': source}
    assert client.post(endpoint + '/events', json=event).status_code == 404
    async def verify():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE evidence SET status='verified' WHERE id=$1", source)
    client.portal.call(verify)
    opened = client.post(endpoint + '/events', json=event).json()
    assert opened == {'counted': True, 'url': 'https://example.test/course'}
    client.put(endpoint, headers=owner, json={'enabled': False})
    before = client.get(endpoint, headers=owner).json()['days']
    client.post(endpoint + '/events', json=event)
    assert client.get(endpoint, headers=owner).json()['days'] == before
    client.delete(endpoint, headers=owner)
    assert client.get(endpoint, headers=owner).json()['days'] == []
    assert client.get(endpoint).status_code == 401
    client.put(endpoint, headers=owner, json={'enabled': True})
    client.patch('/api/me', headers=owner, json={'display_name': 'Owner', 'discoverable': False})
    private_share = client.post('/api/shares', headers=owner).json()['id']
    assert client.post(endpoint + '/events', json={'context': 'share', 'identifier': private_share, 'kind': 'profile_view'}).json()['counted'] is False

def test_recruiter_tray_persistence_density_ownership_and_withdrawal(client):
    recruiter = account(client, 'explore-recruiter@example.test', 'explore-recruiter')
    other = account(client, 'explore-other@example.test', 'explore-other')
    candidate = account(client, 'explore-candidate@example.test', 'explore-candidate')
    uid = client.get('/api/me', headers=candidate).json()['profile']['id']
    endpoint = '/api/recruiter-exploration'
    body = {'revision': 0, 'density': 'compact', 'candidate_ids': [uid]}
    assert client.put(endpoint, headers=recruiter, json=body).status_code == 404
    client.patch('/api/me', headers=candidate, json={'display_name': 'Visible candidate', 'discoverable': True})
    assert client.put(endpoint, headers=recruiter, json=body).status_code == 200
    saved = client.get(endpoint, headers=recruiter).json()
    assert saved['revision'] == 1 and saved['density'] == 'compact'
    assert saved['candidates'][0]['profile']['display_name'] == 'Visible candidate'
    assert client.get(endpoint, headers=other).json()['candidate_ids'] == []
    assert client.put(endpoint, headers=recruiter, json=body).status_code == 409
    assert client.put(endpoint, headers=recruiter, json={**body, 'revision': 1, 'candidate_ids': [uid, uid]}).status_code == 422
    client.patch('/api/me', headers=candidate, json={'display_name': 'Hidden candidate', 'discoverable': False})
    hidden = client.get(endpoint, headers=recruiter).json()
    assert hidden['candidates'] == [] and hidden['unavailable_count'] == 1
    assert 'Hidden candidate' not in str(hidden)
    assert client.put(endpoint, headers=recruiter, json={**body, 'revision': 1}).status_code == 404
    assert client.put(endpoint, headers=recruiter, json={**body, 'revision': 1, 'candidate_ids': []}).status_code == 200
    assert client.get(endpoint).status_code == 401

def test_lineage_preserves_exact_source_and_publication_after_withdrawal(client):
    import hashlib
    owner = account(client, 'lineage-owner@example.test', 'lineage-owner')
    other = account(client, 'lineage-other@example.test', 'lineage-other')
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Historical source', 'url': 'https://example.test/source', 'detail': {'version': 1}}).json()['id']
    path = '/api/lineage/' + source
    before = client.get(path, headers=owner).json()
    assert len(before['revisions']) == 1
    assert before['revisions'][0]['operation'] == 'INSERT'
    async def verify():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE evidence SET status='verified',updated_at=NOW() WHERE id=$1", source)
    client.portal.call(verify)
    draft = client.get('/api/case-studies', headers=owner).json()
    draft['sections'][0]['text'] = 'Claim bound to a reviewed source'
    draft['sections'][0]['evidence_ids'] = [source]
    saved = client.put('/api/case-studies', headers=owner, json=draft).json()
    preview = client.get('/api/case-studies/preview', headers=owner).json()
    assert client.post('/api/case-studies/shares', headers=owner, json={'revision': saved['revision'], 'sha256': preview['sha256'], 'confirm_public': True}).status_code == 201
    traced = client.get(path, headers=owner).json()
    claim = next(r for r in traced['downstream'] if r['kind'] == 'published_claim')
    assert len(claim['source_revision_ids']) == 1
    original = next(r for r in traced['revisions'] if r['id'] == claim['source_revision_ids'][0])
    assert original['payload']['title'] == 'Historical source'
    assert hashlib.sha256(original['canonical_text'].encode()).hexdigest() == original['sha256']
    assert client.get(path, headers=other).status_code == 404
    assert client.get('/api/lineage', headers=other).json() == []
    assert client.delete('/api/evidence/' + source, headers=owner).status_code == 200
    withdrawn = client.get(path, headers=owner).json()
    assert withdrawn['withdrawn'] and not withdrawn['currently_eligible']
    assert withdrawn['revisions'][-1]['operation'] == 'DELETE'
    assert original in withdrawn['revisions']
    assert withdrawn['downstream'][0]['source_revision_ids'] == claim['source_revision_ids']

    async def simulate_legacy_publication():
        from app.platform import db
        async with db() as c:
            await c.execute(
                """UPDATE case_study_publications
                   SET data = data #- '{sections,0,evidence,0,source_revision}'
                                   #- '{sections,0,evidence,0,snapshot_sha256}'
                   WHERE share_id=$1""",
                claim['id'],
            )

    client.portal.call(simulate_legacy_publication)
    legacy = client.get(path, headers=owner)
    assert legacy.status_code == 200
    reference = next(r for r in legacy.json()['downstream'] if r['kind'] == 'published_claim')
    assert reference['id'] == claim['id']
    assert reference['source_revision_ids'] == []
    assert 'exact source revision unavailable' in reference['binding']
    assert client.get(path, headers=other).status_code == 404

def test_dispute_independence_correction_stale_source_and_history(client):
    owner = account(client, 'appeal-owner@example.test', 'appeal-owner')
    first = account(client, 'appeal-first@example.test', 'appeal-first')
    second = account(client, 'appeal-second@example.test', 'appeal-second')
    outsider = account(client, 'appeal-outsider@example.test', 'appeal-outsider')
    async def grant():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE users SET role='reviewer' WHERE email IN ('appeal-first@example.test','appeal-second@example.test')")
    client.portal.call(grant)
    second_id = client.get('/api/me', headers=second).json()['profile']['id']
    first_id = client.get('/api/me', headers=first).json()['profile']['id']
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Appealed source', 'url': 'https://example.test/source', 'detail': {}}).json()['id']
    assert client.post('/api/reviews/' + source, headers=first, json={'status': 'rejected', 'note': 'Initial review rejected the evidence'}).status_code == 200
    body = {'evidence_id': source, 'reason': 'The source was incorrectly interpreted', 'requested_correction': 'Review the original issuer record', 'supporting_ids': [source]}
    result = client.post('/api/disputes', headers=owner, json=body)
    assert result.status_code == 201, result.text
    did = result.json()['id']; path = '/api/disputes/' + did
    assert client.post('/api/disputes', headers=owner, json=body).status_code == 409
    assert client.get(path, headers=outsider).status_code == 404
    revision = 1
    def action(headers, kind, **extra):
        return client.post(path + '/actions', headers=headers, json={'revision': revision, 'action': kind, 'note': 'Reviewed the complete source and supporting material', **extra})
    assert action(owner, 'corrected', corrected_status='verified').status_code == 403
    assert action(second, 'assign', reviewer_id=first_id).status_code == 403
    assert action(second, 'assign', reviewer_id=second_id).status_code == 200
    revision = 2
    assert action(second, 'under_review').status_code == 200
    revision = 3
    assert action(second, 'awaiting_information').status_code == 200
    revision = 4
    assert action(owner, 'respond').status_code == 200
    revision = 5
    assert action(second, 'corrected', corrected_status='verified', source_sha256='stale').status_code == 409
    current = client.get(path, headers=second).json()
    assert action(second, 'corrected', corrected_status='verified', source_sha256=current['source_sha256']).status_code == 200
    completed = client.get(path, headers=owner).json()
    assert completed['state'] == 'corrected' and completed['revision'] == 6
    assert completed['current_source']['status'] == 'verified'
    assert completed['current_source']['eligibility_state'] == 'current'
    assert len(completed['history']) == 6
    assert completed['data']['original_source']['status'] == 'rejected'
    assert action(second, 'upheld').status_code == 409
    async def decisions():
        from app.platform import db
        async with db() as c:
            return [r['status'] for r in await c.fetch('SELECT status FROM review_decisions WHERE evidence_id=$1 ORDER BY id', source)]
    assert client.portal.call(decisions) == ['rejected', 'verified']

def test_organization_roles_invitations_isolation_notes_and_consent(client):
    owner = account(client, 'org-owner@example.test', 'org-owner')
    member = account(client, 'org-member@example.test', 'org-member')
    outsider = account(client, 'org-other@example.test', 'org-other')
    candidate = account(client, 'org-candidate@example.test', 'org-candidate')
    mid = client.get('/api/me', headers=member).json()['profile']['id']
    uid = client.get('/api/me', headers=owner).json()['profile']['id']
    cid = client.get('/api/me', headers=candidate).json()['profile']['id']
    created = client.post('/api/organizations', headers=owner, json={'name': 'Review team'})
    assert created.status_code == 201, created.text
    path = '/api/organizations/' + created.json()['id']
    assert client.get(path, headers=outsider).status_code == 404
    invite = client.post(path + '/invitations', headers=owner, json={'email': 'org-member@example.test', 'role': 'reviewer'}).json()
    assert client.post('/api/organizations/accept', headers=outsider, json={'token': invite['token']}).status_code == 404
    assert client.post('/api/organizations/accept', headers=member, json={'token': invite['token']}).status_code == 200
    assert client.post('/api/organizations/accept', headers=member, json={'token': invite['token']}).status_code == 404
    assert client.post(path + '/invitations', headers=member, json={'email': 'another@example.test'}).status_code == 403
    assert client.delete(path + '/members/' + str(uid), headers=owner).status_code == 403
    note_path = path + '/profiles/' + str(cid)
    assert client.put(note_path, headers=member, json={'revision': 0, 'text': 'Private review note'}).status_code == 404
    client.patch('/api/me', headers=candidate, json={'display_name': 'Candidate', 'discoverable': True})
    assert client.put(note_path, headers=member, json={'revision': 0, 'text': 'Private review note'}).status_code == 200
    assert client.put(note_path, headers=member, json={'revision': 0, 'text': 'Overwrite'}).status_code == 409
    assert client.put(path + '/comparison', headers=member, json={'revision': 0, 'candidate_ids': [cid]}).status_code == 200
    workspace = client.get(path, headers=owner).json()
    assert workspace['profiles'][0]['note'] == 'Private review note'
    assert workspace['candidate_ids'] == [cid]
    assert 'Private review note' not in client.get('/api/me', headers=candidate).text
    client.patch('/api/me', headers=candidate, json={'display_name': 'Hidden candidate', 'discoverable': False})
    withdrawn = client.get(path, headers=member).json()
    assert withdrawn['profiles'] == [] and withdrawn['candidate_ids'] == [] and withdrawn['unavailable_count'] == 1
    assert 'Private review note' not in str(withdrawn)
    assert client.delete(note_path, headers=member).status_code == 200
    assert client.get(path, headers=owner).json()['unavailable_count'] == 0
    assert client.put(path + '/members/' + str(mid), headers=owner, json={'role': 'administrator'}).status_code == 200
    assert client.post(path + '/invitations', headers=member, json={'email': 'another@example.test', 'role': 'administrator'}).status_code == 403
    pending = client.post(path + '/invitations', headers=owner, json={'email': 'org-member@example.test'}).json()
    assert client.delete(path + '/members/' + str(mid), headers=owner).status_code == 200
    assert client.get(path, headers=member).status_code == 404
    assert client.put(note_path, headers=member, json={'revision': 1, 'text': 'Unauthorized'}).status_code == 404
    assert client.post('/api/organizations/accept', headers=member, json={'token': pending['token']}).status_code == 404
    assert client.get('/api/organizations', headers=member).json() == []

def test_question_versions_freeze_grading_and_require_reviewer_confirmation(client):
    candidate = account(client, 'quality-candidate@example.test', 'quality-candidate')
    editor = account(client, 'quality-editor@example.test', 'quality-editor')
    async def grant():
        from app.platform import db
        async with db() as c:
            await c.execute("UPDATE users SET role='reviewer' WHERE email='quality-editor@example.test'")
    client.portal.call(grant)
    root = '/api/assessment-quality/docker'
    assert client.get(root + '/catalog', headers=candidate).status_code == 403
    active = client.post('/api/challenges/docker/attempts', headers=candidate).json()
    assert all('answer' not in q for q in active['questions'])
    config = client.get(root + '/catalog', headers=editor).json()
    old_answer = config['questions'][0]['answer']
    config['questions'][0]['answer'] = (old_answer + 1) % 4
    config['questions'][0]['prompt'] = 'Revised fixture question wording'
    body = {**config, 'reason': 'Synthetic scoring-key revision for isolated verification', 'confirm_publication': False}
    assert client.post(root + '/versions', headers=editor, json=body).status_code == 400
    body['confirm_publication'] = True
    assert client.post(root + '/versions', headers=editor, json=body).status_code == 201
    assert client.post(root + '/versions', headers=editor, json=body).status_code == 409
    from app.domain import CATALOG
    answers = {str(i): q[2] for i, q in enumerate(CATALOG['docker'][2])}
    graded = client.post('/api/attempts/' + active['id'] + '/submit', headers=candidate, json={'answers': answers})
    assert graded.status_code == 200 and graded.json()['correct_count'] == 5
    next_attempt = client.post('/api/challenges/docker/attempts', headers=candidate).json()
    assert any(q['prompt'] == 'Revised fixture question wording' for q in next_attempt['questions'])
    next_grade = client.post('/api/attempts/' + next_attempt['id'] + '/submit', headers=candidate, json={'answers': answers})
    assert next_grade.json()['correct_count'] == 4
    assert client.get(root + '/report', headers=candidate).status_code == 403
    report = client.get(root + '/report', headers=editor).json()
    assert report['suppressed'] and 'items' not in report
    assert len(report['input_sha256']) == 64
    from app.platform import LIMITS
    for index in range(19):
        LIMITS.clear()
        handle = f'quality-cohort-{index}'
        member = account(client, handle+'@example.test', handle)
        attempt = client.post('/api/challenges/docker/attempts', headers=member).json()
        graded = client.post('/api/attempts/'+attempt['id']+'/submit', headers=member, json={'answers': answers})
        assert graded.status_code == 200 and graded.json()['correct_count'] == 4
    published_report = client.get(root + '/report', headers=editor).json()
    assert published_report['eligible_completed'] == 20
    assert not published_report['suppressed'] and published_report['unstable']
    assert [item['observed_correct_fraction'] for item in published_report['items']] == [0, 1, 1, 1, 1]
    assert client.get(root + '/report?version=1', headers=editor).json()['suppressed']

    from concurrent.futures import ThreadPoolExecutor
    from statistics import median
    from time import perf_counter

    def read_reports(_):
        started = perf_counter()
        report = client.get(root + '/report', headers=editor)
        assert report.status_code == 200, report.text
        data = report.json()
        assert data['eligible_completed'] == 20
        assert data['input_sha256'] == published_report['input_sha256']
        assert data['items'] == published_report['items']
        elapsed = (perf_counter() - started) * 1000
        assert client.get(root + '/report', headers=candidate).status_code == 403
        return elapsed

    with ThreadPoolExecutor(max_workers=8) as pool:
        report_times = sorted(pool.map(read_reports, range(64)))
    p95 = report_times[(95 * len(report_times) + 99) // 100 - 1]
    print(f'64 assessment reports at up to 8 concurrent workers: p50={median(report_times):.1f} ms, p95={p95:.1f} ms')

    config = client.get(root + '/catalog', headers=editor).json()
    config['questions'][0]['retired'] = True
    assert client.post(root + '/versions', headers=editor, json={**config, 'reason': 'Retire fixture question pending human revision', 'confirm_publication': True}).status_code == 201
    assert client.post('/api/challenges/docker/attempts', headers=candidate).status_code == 409
    replay = client.post('/api/attempts/' + active['id'] + '/submit', headers=candidate, json={'answers': answers})
    assert replay.json()['correct_count'] == 5

def test_private_portability_archive_files_retry_and_retention(client):
    import io, json, hashlib, time, zipfile
    from pathlib import Path
    from app.config import settings
    from app.portability import work_once
    owner = account(client, 'export-owner@example.test', 'export-owner')
    other = account(client, 'export-other@example.test', 'export-other')
    evidence = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Owned proof', 'url': 'https://example.test/owned', 'detail': {}}).json()['id']
    proof = b'%PDF-1.4\nSynthetic portability fixture\n%%EOF'
    assert client.post('/api/evidence/' + evidence + '/file', headers=owner, files={'file': ('proof.pdf', proof, 'application/pdf')}).status_code == 200
    assert client.put('/api/portability/retention', headers=owner, json={'export_days': 1}).status_code == 200
    response = client.post('/api/portability/exports', headers=owner)
    assert response.status_code == 202
    jid = response.json()['id']
    def wait_status(expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            client.portal.call(work_once)
            jobs = client.get('/api/portability', headers=owner).json()['jobs']
            status = next(r for r in jobs if r['id'] == jid)
            if status['status'] == expected: return status
            if status['status'] == 'failed' and expected != 'failed': pytest.fail(status['error'])
            time.sleep(.1)
        pytest.fail('Archive did not reach ' + expected)
    ready = wait_status('ready')
    path = '/api/portability/exports/' + jid + '/download'
    assert client.get(path, headers=other).status_code == 404
    payload = client.get(path, headers=owner).content
    assert hashlib.sha256(payload).hexdigest() == ready['sha256']
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        records = json.loads(bundle.read('records.json'))
        manifest = json.loads(bundle.read('manifest.json'))
        assert records['users'][0]['email'] == 'export-owner@example.test'
        assert len(records['users']) == 1
        assert 'password_hash' not in records['users'][0]
        assert 'sessions' not in records and 'oauth_states' not in records
        assert 'storage_key' not in records['evidence'][0]
        entry = manifest['files'][0]
        assert bundle.read(entry['archive_path']) == proof
        assert hashlib.sha256(proof).hexdigest() == entry['sha256']
        assert hashlib.sha256(bundle.read('records.json')).hexdigest() == manifest['records_sha256']
    async def interrupt():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE portability_jobs SET status='running' WHERE id=$1", jid)
    client.portal.call(interrupt)
    wait_status('ready')
    async def expire():
        from app.platform import db
        async with db() as c: await c.execute("UPDATE portability_jobs SET expires_at=NOW()-INTERVAL '1 minute' WHERE id=$1", jid)
    client.portal.call(expire)
    wait_status('expired')
    assert client.get(path, headers=owner).status_code == 404
    assert not (Path(settings.storage_path) / 'exports' / (jid + '.zip')).exists()
    assert client.get('/api/portability', headers=other).json()['jobs'] == []
    async def source_key():
        from app.platform import db
        async with db() as c:
            return await c.fetchval('SELECT storage_key FROM evidence WHERE id=$1', evidence)
    source_path = (Path(settings.storage_path) / client.portal.call(source_key)).resolve()
    assert source_path.is_relative_to(Path(settings.storage_path).resolve())
    source_path.unlink()
    jid = client.post('/api/portability/exports', headers=owner).json()['id']
    wait_status('failed')
    source_path.write_bytes(proof)
    assert client.post('/api/portability/exports/' + jid + '/retry', headers=owner).status_code == 200
    wait_status('ready')


def test_account_deletion_reauth_revocation_files_and_restore_tombstone(client):
    import time
    from pathlib import Path
    from app.config import settings
    from app.account_deletion import deletion_once, apply_tombstones
    owner = account(client, 'delete-owner@example.test', 'delete-owner')
    other = account(client, 'delete-other@example.test', 'delete-other')
    uid = client.get('/api/me', headers=owner).json()['profile']['id']
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Delete fixture', 'url': 'https://example.test/source', 'detail': {}}).json()['id']
    assert client.post('/api/evidence/' + source + '/file', headers=owner, files={'file': ('proof.pdf', b'%PDF-1.4 deletion fixture', 'application/pdf')}).status_code == 200
    client.patch('/api/me', headers=owner, json={'display_name': 'Delete fixture', 'discoverable': True})
    share = client.post('/api/shares', headers=owner).json()['id']
    async def before():
        from app.platform import db
        async with db() as c:
            return dict(await c.fetchrow('SELECT * FROM users WHERE id=$1', uid)), await c.fetchval('SELECT storage_key FROM evidence WHERE id=$1', source)
    restored, key = client.portal.call(before)
    preview = client.get('/api/account-deletion/preview', headers=owner).json()
    request = {'preview_sha256': preview['preview_sha256'], 'password': 'wrong-password', 'confirmation': 'DELETE MY ACCOUNT'}
    assert client.post('/api/account-deletion', headers=owner, json=request).status_code == 403
    request['password'] = 'integration-password-123'
    started = client.post('/api/account-deletion', headers=owner, json=request)
    assert started.status_code == 202, started.text
    receipt = {k: started.json()[k] for k in ('id','receipt')}
    assert client.get('/api/me', headers=owner).status_code == 401
    assert client.get('/api/share/' + share).status_code == 404
    assert client.post('/api/auth/login', json={'email': 'delete-owner@example.test', 'password': 'integration-password-123'}).status_code == 401
    assert client.post('/api/account-deletion/status', json={**receipt, 'receipt': 'x'*32}).status_code == 404
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        client.portal.call(deletion_once)
        status = client.post('/api/account-deletion/status', json=receipt).json()
        if status['status'] == 'completed': break
        if status['status'] == 'failed': pytest.fail(status['error'])
        time.sleep(.1)
    assert status['status'] == 'completed'
    assert not (Path(settings.storage_path) / key).exists()
    assert client.get('/api/me', headers=other).status_code == 200
    async def restore_fixture():
        from app.platform import db
        async with db() as c:
            await c.execute("INSERT INTO users(id,handle,email,display_name,password_hash,account_key,account_state,discoverable) VALUES($1,$2,$3,$4,$5,$6,'active',TRUE)", uid, restored['handle'], restored['email'], restored['display_name'], restored['password_hash'], restored['account_key'])
            await c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES($1,$2,NOW()+INTERVAL '1 day')", 'restore-token-fixture', uid)
    client.portal.call(restore_fixture)
    client.portal.call(apply_tombstones)
    async def inspect_restore():
        from app.platform import db
        async with db() as c:
            return await c.fetchval('SELECT account_state FROM users WHERE id=$1', uid), await c.fetchval('SELECT COUNT(*) FROM sessions WHERE user_id=$1', uid)
    state, sessions = client.portal.call(inspect_restore)
    assert state in ('deleting', None) and sessions == 0
    client.portal.call(deletion_once)

def test_organization_owner_must_transfer_before_deletion(client):
    owner = account(client, 'owner-transfer@example.test', 'owner-transfer')
    member = account(client, 'member-transfer@example.test', 'member-transfer')
    mid = client.get('/api/me', headers=member).json()['profile']['id']
    oid = client.post('/api/organizations', headers=owner, json={'name': 'Transfer fixture'}).json()['id']
    path = '/api/organizations/' + oid
    token = client.post(path + '/invitations', headers=owner, json={'email': 'member-transfer@example.test'}).json()['token']
    client.post('/api/organizations/accept', headers=member, json={'token': token})
    preview = client.get('/api/account-deletion/preview', headers=owner).json()
    assert preview['owned_organizations'][0]['id'] == oid
    body = {'preview_sha256': preview['preview_sha256'], 'password': 'integration-password-123', 'confirmation': 'DELETE MY ACCOUNT'}
    assert client.post('/api/account-deletion', headers=owner, json=body).status_code == 409
    assert client.post(path + '/transfer-owner', headers=member, json={'new_owner_id': mid, 'confirm_transfer': True}).status_code == 403
    assert client.post(path + '/transfer-owner', headers=owner, json={'new_owner_id': mid, 'confirm_transfer': True}).status_code == 200
    assert client.get(path, headers=member).json()['role'] == 'owner'
    preview = client.get('/api/account-deletion/preview', headers=owner).json()
    assert preview['owned_organizations'] == []
    body['preview_sha256'] = preview['preview_sha256']
    response = client.post('/api/account-deletion', headers=owner, json=body)
    assert response.status_code == 202
    from app.account_deletion import deletion_once
    client.portal.call(deletion_once)
    assert client.get(path, headers=member).status_code == 200

def test_deletion_recovery_migration_and_write_guards(client):
    from app.account_deletion import deletion_once
    from app.db import run_migrations
    owner = account(client, 'delete-recover@example.test', 'delete-recover')
    uid = client.get('/api/me', headers=owner).json()['profile']['id']
    source = client.post('/api/evidence', headers=owner, json={'kind': 'certificate', 'title': 'Recovery source', 'url': 'https://example.test/recovery', 'detail': {}}).json()['id']
    async def hold_worker():
        from app.db import get_conn
        c = await get_conn()
        await c.execute('SELECT pg_advisory_lock(73722903)')
        return c
    held = client.portal.call(hold_worker)
    try:
        preview = client.get('/api/account-deletion/preview', headers=owner).json()
        response = client.post('/api/account-deletion', headers=owner, json={'preview_sha256': preview['preview_sha256'], 'password': 'integration-password-123', 'confirmation': 'DELETE MY ACCOUNT'})
        assert response.status_code == 202
        client.portal.call(run_migrations)
        async def rejected_writes():
            from app.platform import db
            import asyncpg
            async with db() as c:
                with pytest.raises(asyncpg.RaiseError):
                    await c.execute("INSERT INTO activity(user_id,kind,message) VALUES($1,'late','late in-flight write')", uid)
                with pytest.raises(asyncpg.RaiseError):
                    await c.execute('UPDATE users SET discoverable=TRUE WHERE id=$1', uid)
                return await c.fetchval('SELECT account_state FROM users WHERE id=$1', uid)
        assert client.portal.call(rejected_writes) == 'deleting'
    finally:
        async def release_worker():
            from app.db import release_conn
            await held.execute('SELECT pg_advisory_unlock(73722903)')
            await release_conn(held)
        client.portal.call(release_worker)
    client.portal.call(deletion_once)
    receipt = {k: response.json()[k] for k in ('id','receipt')}
    assert client.post('/api/account-deletion/status', json=receipt).json()['status'] == 'completed'
