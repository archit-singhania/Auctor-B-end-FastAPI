"""Authenticated Auctor platform. Every private resource is scoped to a session."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode
from urllib.parse import urlsplit
import hashlib

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from app.config import settings
from app.db import get_conn, release_conn
from app.domain import CATALOG, check_password, grade, hash_password, public_question, score_from, token_hash
from app.services.cv_parser import CvParserService
from app.insights import build_insights, signal_snapshot

router = APIRouter(prefix='/api')
LIMITS={}
EMPTY_CV={'skills':[], 'projects':[], 'experience':[], 'profiles':{}}

def decode(value):
    return json.loads(value) if isinstance(value,str) else value

def record(row):
    out=dict(row)
    for k in ('data','source','detail','preferences','github_identity','score','signals','question_ids','answers'):
        if k in out and out[k] is not None: out[k]=decode(out[k])
    out.pop('password_hash',None)
    out.pop('run_token',None)
    if 'storage_key' in out: out['has_file']=bool(out['storage_key'])
    out.pop('storage_key',None)
    return out

@asynccontextmanager
async def db():
    c=await get_conn()
    try: yield c
    finally: await release_conn(c)

async def current_user(request: Request):
    authorization=request.headers.get('authorization','')
    if not authorization.startswith('Bearer '): raise HTTPException(401,'Sign in to continue')
    async with db() as c:
        row=await c.fetchrow('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=$1 AND s.expires_at>NOW()',token_hash(authorization[7:]))
    if not row: raise HTTPException(401,'Session expired. Sign in again')
    return record(row)

async def reviewer(user=Depends(current_user)):
    if user['role']!='reviewer': raise HTTPException(403,'Reviewer account required')
    return user

async def event(c,uid,kind,message):
    await c.execute('INSERT INTO activity(user_id,kind,message) VALUES($1,$2,$3)',uid,kind,message)

async def cv_data(c,uid):
    row=await c.fetchrow('SELECT data FROM cv_versions WHERE user_id=$1 ORDER BY id DESC LIMIT 1',uid)
    return decode(row['data']) if row else dict(EMPTY_CV)

async def recalculate(c,uid,reason):
    evidence=[record(r) for r in await c.fetch('SELECT * FROM evidence WHERE user_id=$1',uid)]
    badges=[r['badge_id'] for r in await c.fetch('SELECT DISTINCT badge_id FROM attempts WHERE user_id=$1 AND passed=TRUE',uid)]
    data=await cv_data(c,uid)
    weights={k:getattr(settings,'weight_'+k) for k in ('github','leetcode','badges','projects','experience')}
    score=score_from(evidence,badges,data.get('projects',[]),weights)
    signals=signal_snapshot(data,evidence,badges)
    last=await c.fetchrow('SELECT score,signals FROM score_history WHERE user_id=$1 ORDER BY id DESC LIMIT 1',uid)
    if last is None or decode(last['score'])!=score or decode(last['signals'])!=signals:
        await c.execute('INSERT INTO score_history(user_id,score,reason,signals) VALUES($1,$2::jsonb,$3,$4::jsonb)',uid,json.dumps(score),reason,json.dumps(signals))
    return score

class Credentials(BaseModel):
    email:str=Field(min_length=5,max_length=254)
    password:str=Field(min_length=10,max_length=128)
    handle:str=Field(default='',max_length=32)
    display_name:str=Field(default='',max_length=80)

    @model_validator(mode='after')
    def validate_email(self):
        self.email=self.email.strip().lower()
        if self.email.count('@')!=1 or '.' not in self.email.split('@')[1]: raise ValueError('Enter a valid email')
        return self

class ProfilePatch(BaseModel):
    display_name:str=Field(max_length=80)
    bio:str=Field(default='',max_length=1000)
    discoverable:bool=False
    preferences:dict=Field(default_factory=dict)

class CvPatch(BaseModel):
    data:dict
    note:str=Field(default='Edited CV',max_length=200)

    @model_validator(mode='after')
    def valid(self):
        from app.models.cv import ExtractedCvData
        self.data=ExtractedCvData.model_validate(self.data).model_dump()
        for item in self.data['projects']+self.data['experience']: item['is_verified']=False
        if len(json.dumps(self.data))>100000: raise ValueError('CV data too large')
        return self

class EvidenceInput(BaseModel):
    kind:str
    title:str=Field(min_length=1,max_length=200)
    url:str=Field(default='',max_length=2000)
    detail:dict=Field(default_factory=dict)

class Decision(BaseModel):
    status:str
    note:str=Field(min_length=10,max_length=2000)

class Answers(BaseModel):
    answers:dict[str,int]

async def session(c,uid):
    value=secrets.token_urlsafe(40)
    await c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES($1,$2,NOW()+INTERVAL '30 days')",token_hash(value),uid)
    return {'token':value,'expires_in':2592000}

@router.post('/auth/register',status_code=201)
async def register(payload:Credentials,request:Request):
    rate_limit(request,'auth',8)
    import re
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,31}',payload.handle): raise HTTPException(422,'Handle: 3–32 lowercase letters, numbers or hyphens')
    # Email possession is not proven by password signup; roles require operator assignment.
    role='developer'
    encoded=await asyncio.to_thread(hash_password,payload.password)
    async with db() as c:
        try:
            async with c.transaction():
                uid=await c.fetchval('INSERT INTO users(handle,email,display_name,password_hash,role) VALUES($1,$2,$3,$4,$5) RETURNING id',payload.handle,payload.email,payload.display_name or payload.handle,encoded,role)
                result=await session(c,uid)
                await event(c,uid,'account','Your private evidence workspace is ready')
        except Exception as e:
            if getattr(e,'sqlstate',None)=='23505': raise HTTPException(409,'Email or handle already registered') from e
            raise
    return result

def rate_limit(request,kind,maximum=60):
    key=(request.client.host if request.client else 'unknown',kind)
    now=time.monotonic()
    visits=[t for t in LIMITS.get(key,[]) if now-t<60]
    if len(visits)>=maximum: raise HTTPException(429,'Too many requests. Try again in a minute')
    LIMITS[key]=visits+[now]
    if len(LIMITS)>10000:
        expired=[k for k,v in LIMITS.items() if not v or now-v[-1]>60]
        for k in expired: LIMITS.pop(k,None)

@router.post('/auth/login')
async def login(payload:Credentials,request:Request):
    rate_limit(request,'auth',8)
    async with db() as c:
        row=await c.fetchrow('SELECT id,password_hash FROM users WHERE email=$1',payload.email)
        encoded=row['password_hash'] if row and row['password_hash'] else 'pbkdf2_sha256$600000$invalid$invalid'
        valid=await asyncio.to_thread(check_password,payload.password,encoded)
        if not row or not valid: raise HTTPException(401,'Email or password is incorrect')
        return await session(c,row['id'])

@router.post('/auth/logout')
async def logout(request:Request,user=Depends(current_user)):
    async with db() as c: await c.execute('DELETE FROM sessions WHERE token_hash=$1',token_hash(request.headers['authorization'][7:]))
    return {'ok':True}

@router.get('/me')
async def me(user=Depends(current_user)):
    async with db() as c:
        uid=user['id']
        score=await recalculate(c,uid,'Workspace refreshed')
        workspace={'profile':user,'cv':await cv_data(c,uid),'score':score,'evidence':[record(r) for r in await c.fetch('SELECT * FROM evidence WHERE user_id=$1 ORDER BY created_at DESC',uid)],'documents':[record(r) for r in await c.fetch('SELECT * FROM documents WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30',uid)],'versions':[record(r) for r in await c.fetch('SELECT * FROM cv_versions WHERE user_id=$1 ORDER BY id DESC LIMIT 30',uid)],'badges':[record(r) for r in await c.fetch('SELECT id,badge_id,started_at,submitted_at,correct_count,passed,score_delta FROM attempts WHERE user_id=$1 ORDER BY started_at DESC LIMIT 50',uid)],'history':[record(r) for r in await c.fetch('SELECT * FROM score_history WHERE user_id=$1 ORDER BY id DESC LIMIT 30',uid)],'activity':[record(r) for r in await c.fetch('SELECT * FROM activity WHERE user_id=$1 ORDER BY id DESC LIMIT 50',uid)],'shares':[record(r) for r in await c.fetch('SELECT * FROM shares WHERE user_id=$1 ORDER BY created_at DESC',uid)]}
        workspace['earned_badges']=[r['badge_id'] for r in await c.fetch('SELECT DISTINCT badge_id FROM attempts WHERE user_id=$1 AND passed=TRUE',uid)]
        workspace['insights']=build_insights(workspace['cv'],workspace['evidence'],[{'badge_id':b,'passed':True} for b in workspace['earned_badges']],user.get('github_identity',{}),workspace['history'])
        workspace['review_audit']=[record(r) for r in await c.fetch('SELECT * FROM review_decisions WHERE user_id=$1 ORDER BY id DESC LIMIT 100',uid)]
        return workspace

@router.patch('/me')
async def edit_profile(payload:ProfilePatch,user=Depends(current_user)):
    if payload.preferences.get('theme','system') not in ('system','light','dark'): raise HTTPException(422,'Unknown theme')
    if len(json.dumps(payload.preferences))>4096: raise HTTPException(422,'Appearance preferences are too large')
    for key in ('reduced_motion','reduced_transparency','high_contrast'):
        if key in payload.preferences and type(payload.preferences[key]) is not bool:
            raise HTTPException(422,'Accessibility preferences must be true or false')
    async with db() as c:
        await c.execute('UPDATE users SET display_name=$2,bio=$3,discoverable=$4,preferences=$5::jsonb WHERE id=$1',user['id'],payload.display_name,payload.bio,payload.discoverable,json.dumps(payload.preferences))
    return {'ok':True}

@router.put('/cv')
async def edit_cv(payload:CvPatch,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.execute('INSERT INTO cv_versions(user_id,data,note) VALUES($1,$2::jsonb,$3)',user['id'],json.dumps(payload.data),payload.note)
            score=await recalculate(c,user['id'],payload.note)
            await event(c,user['id'],'cv',payload.note)
    return {'data':payload.data,'score':score}

@router.post('/cv/versions/{version_id}/restore')
async def restore_cv(version_id:int,user=Depends(current_user)):
    async with db() as c:
        row=await c.fetchrow('SELECT data FROM cv_versions WHERE id=$1 AND user_id=$2',version_id,user['id'])
        if not row: raise HTTPException(404,'Version not found')
    return await edit_cv(CvPatch(data=decode(row['data']),note=f'Restored version {version_id}'),user)

async def save_file(file,folder):
    content=await file.read(10*1024*1024+1)
    if not content: raise HTTPException(400,'Choose a nonempty PDF')
    if len(content)>10*1024*1024: raise HTTPException(413,'PDF limit is 10 MB')
    if not content.startswith(b'%PDF-'): raise HTTPException(400,'Only valid PDF files are accepted')
    root=Path(settings.storage_path).resolve()
    key=f'{folder}/{secrets.token_hex(24)}.pdf'
    dest=root/key
    await asyncio.to_thread(dest.parent.mkdir,parents=True,exist_ok=True)
    await asyncio.to_thread(dest.write_bytes,content)
    if settings.app_env == 'production':
        import os
        if os.name != 'nt': await asyncio.to_thread(dest.chmod,0o600)
    return key

@router.post('/cv/jobs',status_code=202)
async def upload_cv(request:Request,file:UploadFile=File(...),user=Depends(current_user)):
    rate_limit(request,'parse',5)
    key=await save_file(file,'cv')
    jid=secrets.token_urlsafe(18)
    async with db() as c:
        await c.execute('INSERT INTO documents(id,user_id,filename,storage_key) VALUES($1,$2,$3,$4)',jid,user['id'],(file.filename or 'Resume.pdf')[:200],key)
        await event(c,user['id'],'cv','CV queued for extraction')
    return {'id':jid,'status':'queued'}

@router.get('/cv/jobs/{job_id}')
async def job_status(job_id:str,user=Depends(current_user)):
    async with db() as c: row=await c.fetchrow('SELECT * FROM documents WHERE id=$1 AND user_id=$2',job_id,user['id'])
    if not row: raise HTTPException(404,'Job not found')
    return record(row)

@router.get('/cv/jobs/{job_id}/file')
async def cv_source(job_id:str,user=Depends(current_user)):
    async with db() as c: row=await c.fetchrow('SELECT storage_key FROM documents WHERE id=$1 AND user_id=$2',job_id,user['id'])
    if not row: raise HTTPException(404,'Source document not found')
    content=await asyncio.to_thread((Path(settings.storage_path)/row['storage_key']).read_bytes)
    return Response(content,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="cv-source.pdf"','Cache-Control':'no-store'})

@router.post('/cv/jobs/{job_id}/{action}')
async def job_action(job_id:str,action:str,user=Depends(current_user)):
    if action not in ('cancel','retry'): raise HTTPException(404,'Unknown job action')
    async with db() as c:
        async with c.transaction():
            row=await c.fetchrow('SELECT status FROM documents WHERE id=$1 AND user_id=$2 FOR UPDATE',job_id,user['id'])
            if not row: raise HTTPException(404,'Job not found')
            if action=='retry' and row['status'] not in ('failed','cancelled'): raise HTTPException(409,'Only failed or cancelled jobs can retry')
            if action=='cancel' and row['status'] not in ('queued','running'): raise HTTPException(409,'Job has already finished')
            await c.execute('UPDATE documents SET status=$2,error=NULL,updated_at=NOW() WHERE id=$1',job_id,'cancelled' if action=='cancel' else 'queued')
    return {'ok':True}

async def job_worker():
    parser=CvParserService()
    while True:
        try:
            async with db() as c:
                async with c.transaction():
                    await c.execute("UPDATE documents SET status='queued',updated_at=NOW() WHERE status='running' AND updated_at<NOW()-INTERVAL '5 minutes'")
                    job=await c.fetchrow("SELECT * FROM documents WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
                    if job:
                        run_token=secrets.token_hex(16)
                        await c.execute("UPDATE documents SET status='running',run_token=$2,updated_at=NOW() WHERE id=$1",job['id'],run_token)
            if not job:
                await asyncio.sleep(1)
                continue
            try:
                content=await asyncio.to_thread((Path(settings.storage_path)/job['storage_key']).read_bytes)
                data=(await asyncio.wait_for(parser.parse(content),timeout=120)).model_dump()
                source={'filename':job['filename'],'parser':'AI-assisted with heuristic fallback' if settings.openai_api_key else 'Heuristic','confidence':'Requires your review','verified':False}
                async with db() as c:
                    async with c.transaction():
                        lease=await c.fetchrow('SELECT status,run_token FROM documents WHERE id=$1 FOR UPDATE',job['id'])
                        if lease['status']!='running' or lease['run_token']!=run_token: continue
                        await c.execute("UPDATE documents SET status='succeeded',data=$2::jsonb,source=$3::jsonb,updated_at=NOW() WHERE id=$1",job['id'],json.dumps(data),json.dumps(source))
                        await c.execute('INSERT INTO cv_versions(user_id,document_id,data,note) VALUES($1,$2,$3::jsonb,$4)',job['user_id'],job['id'],json.dumps(data),'CV extraction; review required')
                        await recalculate(c,job['user_id'],'CV extracted')
                        await event(c,job['user_id'],'cv','CV extracted. Review the claims before sharing')
            except Exception:
                async with db() as c:
                    await c.execute("UPDATE documents SET status='failed',error=$2,updated_at=NOW() WHERE id=$1 AND status='running' AND run_token=$3",job['id'],'Extraction failed. Check the PDF is readable text, then retry',run_token)
                    await event(c,job['user_id'],'error','CV extraction failed; the previous version remains available')
        except asyncio.CancelledError: raise
        except Exception:
            await asyncio.sleep(3)

@router.get('/challenges')
async def challenges(user=Depends(current_user)):
    return [{'id':key,'name':value[0],'skill':value[1],'questions':5,'duration_seconds':300,'pass_threshold':3} for key,value in CATALOG.items()]

@router.get('/challenges/{badge_id}')
async def badge_detail(badge_id:str,user=Depends(current_user)):
    if badge_id not in CATALOG: raise HTTPException(404,'Challenge not found')
    async with db() as c:
        attempts=[record(r) for r in await c.fetch('SELECT id,badge_id,started_at,submitted_at,expires_at,correct_count,passed,score_delta FROM attempts WHERE user_id=$1 AND badge_id=$2 ORDER BY started_at DESC LIMIT 100',user['id'],badge_id)]
        earned=await c.fetchval('SELECT EXISTS(SELECT 1 FROM attempts WHERE user_id=$1 AND badge_id=$2 AND passed=TRUE)',user['id'],badge_id)
    return {'id':badge_id,'name':CATALOG[badge_id][0],'skill':CATALOG[badge_id][1],'earned':earned,'questions':5,'pass_threshold':3,'duration_seconds':300,'attempts':attempts,'scope':'A pass covers this five-question assessment. Repeated passes add no score; failed retries preserve an earned badge.'}

@router.post('/challenges/{badge_id}/attempts',status_code=201)
async def start_attempt(badge_id:str,user=Depends(current_user)):
    if badge_id not in CATALOG: raise HTTPException(404,'Challenge not found')
    ids=list(range(5)); secrets.SystemRandom().shuffle(ids)
    async with db() as c:
        # Serialize attempts per account to stop racing the active-attempt guard.
        async with c.transaction():
            await c.execute('SELECT id FROM users WHERE id=$1 FOR UPDATE',user['id'])
            row=await c.fetchrow('SELECT * FROM attempts WHERE user_id=$1 AND badge_id=$2 AND submitted_at IS NULL AND expires_at>NOW() ORDER BY started_at DESC LIMIT 1',user['id'],badge_id)
            if not row:
                recent=await c.fetchval("SELECT COUNT(*) FROM attempts WHERE user_id=$1 AND started_at>NOW()-INTERVAL '1 hour'",user['id'])
                if recent>=10: raise HTTPException(429,'Assessment limit reached. Practice, then return in an hour')
                aid=secrets.token_urlsafe(18)
                row=await c.fetchrow("INSERT INTO attempts(id,user_id,badge_id,question_ids,expires_at) VALUES($1,$2,$3,$4::jsonb,NOW()+INTERVAL '5 minutes') RETURNING *",aid,user['id'],badge_id,json.dumps(ids))
    return {'id':row['id'],'badge_id':badge_id,'expires_at':row['expires_at'],'questions':[public_question(badge_id,i) for i in decode(row['question_ids'])]}

@router.post('/attempts/{attempt_id}/submit')
async def submit_attempt(attempt_id:str,payload:Answers,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.execute('SELECT id FROM users WHERE id=$1 FOR UPDATE',user['id'])
            attempt=await c.fetchrow('SELECT * FROM attempts WHERE id=$1 AND user_id=$2 FOR UPDATE',attempt_id,user['id'])
            if not attempt: raise HTTPException(404,'Attempt not found')
            if attempt['submitted_at']:
                return {'id':attempt_id,'passed':attempt['passed'],'correct_count':attempt['correct_count'],'score_delta':float(attempt['score_delta']),'replayed':True}
            if attempt['expires_at']<datetime.now(timezone.utc): raise HTTPException(409,'Attempt expired. Start another challenge')
            try: correct,passed=grade(attempt['badge_id'],decode(attempt['question_ids']),payload.answers)
            except ValueError as e: raise HTTPException(422,str(e)) from e
            before=await recalculate(c,user['id'],'Before challenge')
            await c.execute('UPDATE attempts SET submitted_at=NOW(),answers=$2::jsonb,correct_count=$3,passed=$4 WHERE id=$1',attempt_id,json.dumps(payload.answers),correct,passed)
            after=await recalculate(c,user['id'],'Challenge '+attempt['badge_id'])
            delta=round(after['total']-before['total'],2)
            await c.execute('UPDATE attempts SET score_delta=$2 WHERE id=$1',attempt_id,delta)
            await event(c,user['id'],'challenge',f"{CATALOG[attempt['badge_id']][0]}: {correct}/5, "+('badge earned' if passed else 'practice and retry'))
    return {'id':attempt_id,'passed':passed,'correct_count':correct,'score_delta':delta,'replayed':False}

@router.post('/evidence',status_code=201)
async def add_evidence(payload:EvidenceInput,user=Depends(current_user)):
    if payload.kind not in ('experience','certificate','coding','project'): raise HTTPException(422,'Unsupported evidence kind')
    if payload.url and not payload.url.startswith(('https://','http://localhost:')): raise HTTPException(422,'Use an HTTPS source URL')
    if len(json.dumps(payload.detail))>10000: raise HTTPException(422,'Evidence details too large')
    if payload.kind=='coding':
        solved=payload.detail.get('solved',0)
        if type(solved) is not int or solved<0 or solved>100000: raise HTTPException(422,'Solved count must be a nonnegative integer')
    if payload.kind=='certificate':
        for field in ('issuer','reference','issued_on'):
            value=payload.detail.get(field,'')
            if not isinstance(value,str) or len(value)>300: raise HTTPException(422,'Certificate issuer/reference/date must be text of at most 300 characters')
    status='pending'
    if payload.kind=='project':
        repository=payload.detail.get('repository','')
        repos=user.get('github_identity',{}).get('repositories',[])
        if not any(r['full_name']==repository for r in repos): raise HTTPException(422,'Choose an owned GitHub repository after connecting your account')
        async with db() as c: cv=await cv_data(c,user['id'])
        if payload.detail.get('project') not in [p['name'] for p in cv.get('projects',[])]: raise HTTPException(422,'Choose a project from your CV')
        payload.detail['provenance']='Owned repository linked by OAuth account; contents are not a skill assessment'
        status='verified'
    eid=secrets.token_urlsafe(18)
    async with db() as c:
        async with c.transaction():
            await c.execute('INSERT INTO evidence(id,user_id,kind,title,url,detail,status) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7)',eid,user['id'],payload.kind,payload.title,payload.url,json.dumps(payload.detail),status)
            await recalculate(c,user['id'],'Evidence added')
            await event(c,user['id'],'evidence',payload.title+' added: '+status)
    return {'id':eid,'status':status}

@router.post('/coding/import',status_code=201)
async def import_coding_profile(file:UploadFile=File(...),user=Depends(current_user)):
    content=await file.read(65537)
    if not content or len(content)>65536: raise HTTPException(413,'Coding profile JSON limit is 64 KB')
    try:
        data=json.loads(content)
        if not isinstance(data,dict): raise ValueError()
        source=data['source_url']; solved=data['solved']
        if not isinstance(source,str): raise ValueError()
        url=urlsplit(source)
        if url.scheme!='https' or url.hostname not in ('leetcode.com','www.leetcode.com','codeforces.com','www.codeforces.com','hackerrank.com','www.hackerrank.com') or url.port not in (None,443) or url.username or url.password or url.query or url.fragment or url.path in ('','/'):
            raise ValueError()
        if type(solved) is not int or not 0<=solved<=100000: raise ValueError()
    except (ValueError,TypeError,KeyError):
        raise HTTPException(422,'Use a profile JSON with a recognized HTTPS source_url and integer solved count')
    detail={'solved':solved,'claimed':True,'import_method':'User-supplied JSON; not a provider-verified count',
            'import_sha256':hashlib.sha256(content).hexdigest(),'imported_at':datetime.now(timezone.utc).isoformat(),'provider':url.hostname}
    return await add_evidence(EvidenceInput(kind='coding',title='Imported coding profile: '+url.hostname,url=source,detail=detail),user)

@router.post('/evidence/{evidence_id}/file')
async def attach_proof(evidence_id:str,file:UploadFile=File(...),user=Depends(current_user)):
    async with db() as c:
        row=await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND user_id=$2',evidence_id,user['id'])
        if not row: raise HTTPException(404,'Evidence not found')
        if row['kind']=='project' or row['status']=='verified': raise HTTPException(409,'Create a new evidence submission to update reviewed proof')
    key=await save_file(file,'proof')
    async with db() as c:
        updated=await c.fetchval("UPDATE evidence SET storage_key=$2,status='pending',updated_at=NOW() WHERE id=$1 AND user_id=$3 AND status<>'verified' AND kind<>'project' RETURNING id",evidence_id,key,user['id'])
        if not updated: raise HTTPException(409,'Evidence was reviewed or removed while the file uploaded. Create a new submission')
    return {'ok':True}

@router.get('/evidence/{evidence_id}/file')
async def proof_file(evidence_id:str,user=Depends(current_user)):
    async with db() as c: row=await c.fetchrow('SELECT * FROM evidence WHERE id=$1',evidence_id)
    if not row or (row['user_id']!=user['id'] and user['role']!='reviewer'): raise HTTPException(404,'Proof not found')
    if not row['storage_key']: raise HTTPException(404,'No proof attached')
    content=await asyncio.to_thread((Path(settings.storage_path)/row['storage_key']).read_bytes)
    return Response(content,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="evidence.pdf"','Cache-Control':'no-store'})

@router.delete('/evidence/{evidence_id}')
async def remove_evidence(evidence_id:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            result=await c.execute('DELETE FROM evidence WHERE id=$1 AND user_id=$2',evidence_id,user['id'])
            if result=='DELETE 0': raise HTTPException(404,'Evidence not found')
            await recalculate(c,user['id'],'Evidence removed')
    return {'ok':True}

@router.get('/reviews')
async def reviews(user=Depends(reviewer)):
    async with db() as c: return [record(r) for r in await c.fetch("SELECT e.*,u.handle FROM evidence e JOIN users u ON u.id=e.user_id WHERE e.status='pending' ORDER BY e.created_at LIMIT 100")]

@router.get('/reviews/audit')
async def review_audit(user=Depends(reviewer)):
    async with db() as c:
        return [record(r) for r in await c.fetch('SELECT d.*,u.handle FROM review_decisions d JOIN users u ON u.id=d.user_id ORDER BY d.id DESC LIMIT 100')]

@router.post('/reviews/{evidence_id}')
async def review(evidence_id:str,payload:Decision,user=Depends(reviewer)):
    if payload.status not in ('verified','rejected'): raise HTTPException(422,'Choose verified or rejected')
    async with db() as c:
        async with c.transaction():
            row=await c.fetchrow("SELECT * FROM evidence WHERE id=$1 AND status='pending' FOR UPDATE",evidence_id)
            if not row: raise HTTPException(404,'Pending submission not found')
            if row['user_id']==user['id']: raise HTTPException(403,'You cannot review your own evidence')
            if not row['storage_key'] and not row['url']: raise HTTPException(422,'Proof file or source URL required for review')
            await c.execute('UPDATE evidence SET status=$2,reviewer_id=$3,review_note=$4,updated_at=NOW() WHERE id=$1',evidence_id,payload.status,user['id'],payload.note)
            source={'url':row['url'],'has_file':bool(row['storage_key']),'detail':decode(row['detail'])}
            await c.execute('INSERT INTO review_decisions(evidence_id,user_id,reviewer_id,title,kind,status,note,source) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)',evidence_id,row['user_id'],user['id'],row['title'],row['kind'],payload.status,payload.note,json.dumps(source))
            await recalculate(c,row['user_id'],'Evidence reviewed')
            await event(c,row['user_id'],'review',row['title']+': '+payload.status+' — '+payload.note)
    return {'ok':True}

@router.post('/github/connect')
async def github_connect(user=Depends(current_user)):
    if not settings.github_client_id or not settings.github_client_secret: raise HTTPException(503,'GitHub OAuth is not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET on the API')
    state=secrets.token_urlsafe(32)
    async with db() as c: await c.execute("INSERT INTO oauth_states(state_hash,user_id,expires_at) VALUES($1,$2,NOW()+INTERVAL '10 minutes')",token_hash(state),user['id'])
    return {'url':'https://github.com/login/oauth/authorize?'+urlencode({'client_id':settings.github_client_id,'redirect_uri':settings.github_redirect_uri,'scope':'read:user','state':state})}

@router.get('/github/callback')
async def github_callback(state:str,code:str):
    async with db() as c:
        row=await c.fetchrow('DELETE FROM oauth_states WHERE state_hash=$1 AND expires_at>NOW() RETURNING user_id',token_hash(state))
    if not row: raise HTTPException(400,'OAuth state expired or already used')
    async with httpx.AsyncClient(timeout=20) as client:
        exchange=await client.post('https://github.com/login/oauth/access_token',headers={'Accept':'application/json'},json={'client_id':settings.github_client_id,'client_secret':settings.github_client_secret,'code':code,'redirect_uri':settings.github_redirect_uri})
        exchange.raise_for_status(); token=exchange.json().get('access_token')
        if not token: raise HTTPException(502,'GitHub authorization failed; reconnect')
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
        identity=await client.get('https://api.github.com/user',headers=headers); identity.raise_for_status()
        profile=identity.json(); repos=[]
        activity_response=await client.get('https://api.github.com/users/'+profile['login']+'/events/public',headers=headers,params={'per_page':100})
        activity=[]
        if activity_response.status_code==200:
            activity=[{'type':e['type'],'repository':e['repo']['name'],'created_at':e['created_at']} for e in activity_response.json()]
        for page in range(1,21):
            response=await client.get('https://api.github.com/user/repos',headers=headers,params={'visibility':'public','affiliation':'owner','per_page':100,'page':page,'sort':'updated'})
            response.raise_for_status(); batch=response.json()
            repos.extend({'name':r['name'],'full_name':r['full_name'],'url':r['html_url'],'language':r.get('language'),'stars':r['stargazers_count'],'fork':r['fork'],'updated_at':r['updated_at']} for r in batch if not r['private'])
            if len(batch)<100: break
    detail={'id':profile['id'],'login':profile['login'],'url':profile['html_url'],'repositories':repos,'recent_activity':activity,'activity_scope':'Up to 100 recent public events; not a complete contribution history','synced_at':datetime.now(timezone.utc).isoformat(),'scope':'Public owned repositories only','refresh':'Reconnect to update the snapshot'}
    async with db() as c:
        async with c.transaction():
            previous_identity=decode(await c.fetchval('SELECT github_identity FROM users WHERE id=$1 FOR UPDATE',row['user_id']))
            if previous_identity.get('id')!=profile['id']:
                await c.execute("DELETE FROM evidence WHERE user_id=$1 AND kind='project'",row['user_id'])
            await c.execute('UPDATE users SET github_identity=$2::jsonb WHERE id=$1',row['user_id'],json.dumps(detail))
            await c.execute("DELETE FROM evidence WHERE user_id=$1 AND kind='github'",row['user_id'])
            await c.execute("INSERT INTO evidence(id,user_id,kind,title,url,detail,status) VALUES($1,$2,'github',$3,$4,$5::jsonb,'verified')",secrets.token_urlsafe(18),row['user_id'],'GitHub account ownership: '+profile['login'],profile['html_url'],json.dumps({'oauth_id':profile['id'],'method':'GitHub OAuth account ownership','synced_at':detail['synced_at']}))
            await recalculate(c,row['user_id'],'GitHub ownership confirmed')
            await event(c,row['user_id'],'github','GitHub ownership confirmed; choose repositories to link projects')
    return RedirectResponse(settings.web_url.rstrip('/')+'/#/workspace?github=connected',status_code=303)

@router.delete('/github')
async def disconnect_github(user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.execute("UPDATE users SET github_identity='{}'::jsonb WHERE id=$1",user['id'])
            await c.execute("DELETE FROM evidence WHERE user_id=$1 AND kind IN ('github','project')",user['id'])
            await recalculate(c,user['id'],'GitHub disconnected')
    return {'ok':True}

@router.post('/activity/read')
async def read_activity(user=Depends(current_user)):
    async with db() as c: await c.execute('UPDATE activity SET read=TRUE WHERE user_id=$1',user['id'])
    return {'ok':True}

@router.post('/shares',status_code=201)
async def share(user=Depends(current_user)):
    sid=secrets.token_urlsafe(24)
    async with db() as c: await c.execute('INSERT INTO shares(id,user_id) VALUES($1,$2)',sid,user['id'])
    return {'id':sid,'url':settings.web_url.rstrip('/')+'/#/share/'+sid}

@router.delete('/shares/{share_id}')
async def revoke(share_id:str,user=Depends(current_user)):
    async with db() as c:
        result=await c.execute('UPDATE shares SET revoked=TRUE WHERE id=$1 AND user_id=$2',share_id,user['id'])
        if result=='UPDATE 0': raise HTTPException(404,'Share not found')
    return {'ok':True}

async def public_data(c,uid):
    profile=await c.fetchrow('SELECT id,handle,display_name,bio FROM users WHERE id=$1',uid)
    data=await cv_data(c,uid)
    evidence=[record(r) for r in await c.fetch("SELECT id,kind,title,url,detail,status,review_note,updated_at FROM evidence WHERE user_id=$1 AND status='verified'",uid)]
    # Private contact information and proof documents never enter public payloads.
    return {'profile':dict(profile),'skills':data.get('skills',[]),'projects':data.get('projects',[]),'score':await recalculate(c,uid,'Public profile refreshed'),'evidence':evidence,'badges':[r['badge_id'] for r in await c.fetch('SELECT DISTINCT badge_id FROM attempts WHERE user_id=$1 AND passed=TRUE',uid)]}

@router.get('/public/{handle}')
async def public_profile(handle:str):
    async with db() as c:
        uid=await c.fetchval('SELECT id FROM users WHERE handle=$1 AND discoverable=TRUE',handle)
        if not uid: raise HTTPException(404,'Profile is private or unavailable')
        return await public_data(c,uid)

@router.get('/share/{share_id}')
async def shared_profile(share_id:str):
    async with db() as c:
        uid=await c.fetchval('SELECT user_id FROM shares WHERE id=$1 AND revoked=FALSE',share_id)
        if not uid: raise HTTPException(404,'Share is unavailable or revoked')
        return await public_data(c,uid)

@router.get('/candidates')
async def candidates(q:str=Query(default='',max_length=100),min_score:float=Query(default=0,ge=0,le=10),user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch("SELECT id FROM users WHERE discoverable=TRUE AND password_hash IS NOT NULL AND (display_name ILIKE $1 OR handle ILIKE $1 OR (SELECT (v.data->'skills')::text FROM cv_versions v WHERE v.user_id=users.id ORDER BY v.id DESC LIMIT 1) ILIKE $1) ORDER BY handle LIMIT 50",'%'+q+'%')
        result=[]
        saved={r['candidate_id'] for r in await c.fetch('SELECT candidate_id FROM saved_candidates WHERE user_id=$1',user['id'])}
        for r in rows:
            item=await public_data(c,r['id'])
            if item['score']['total']>=min_score:
                item['saved']=r['id'] in saved; result.append(item)
    return result

@router.post('/candidates/{candidate_id}/save')
async def save_candidate(candidate_id:int,user=Depends(current_user)):
    async with db() as c:
        if not await c.fetchval('SELECT discoverable FROM users WHERE id=$1',candidate_id): raise HTTPException(404,'Candidate unavailable')
        await c.execute('INSERT INTO saved_candidates(user_id,candidate_id) VALUES($1,$2) ON CONFLICT DO NOTHING',user['id'],candidate_id)
    return {'ok':True}

@router.delete('/candidates/{candidate_id}/save')
async def unsave_candidate(candidate_id:int,user=Depends(current_user)):
    async with db() as c: await c.execute('DELETE FROM saved_candidates WHERE user_id=$1 AND candidate_id=$2',user['id'],candidate_id)
    return {'ok':True}

@router.get('/export')
async def export(format:str='json',user=Depends(current_user)):
    async with db() as c: data=await public_data(c,user['id'])
    if format=='json':
        from fastapi.encoders import jsonable_encoder
        return Response(json.dumps(jsonable_encoder(data),indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="auctor-evidence.json"'})
    if format!='pdf': raise HTTPException(422,'Choose json or pdf')
    from app.evidence_report import evidence_pdf
    return Response(evidence_pdf(data,user),media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="auctor-evidence.pdf"'})

@router.get('/badge/{handle}.svg')
async def embed_badge(handle:str):
    data=await public_profile(handle)
    plain_label=f"{handle} · {data['score']['total']}/10"
    label=html.escape(plain_label)
    width=max(360,145+len(plain_label)*10)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="46" role="img" aria-label="Auctor evidence score"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="#263c3c"/><stop offset="1" stop-color="#20282e"/></linearGradient></defs><rect x=".5" y=".5" width="{width-1}" height="45" rx="13" fill="url(#g)" stroke="#657c75"/><path d="M104 10v26" stroke="#657c75"/><text x="18" y="29" font-family="Inter,system-ui,sans-serif" font-size="12" font-weight="600" letter-spacing="1.2" fill="#d8bd8a">AUCTOR</text><text x="121" y="29" font-family="Inter,system-ui,sans-serif" font-size="12" fill="#f3f0e7">{label}</text></svg>'
    return Response(svg,media_type='image/svg+xml',headers={'Cache-Control':'public,max-age=60'})

