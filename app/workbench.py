"""Private evidence snapshots, text comparisons and ungraded interview practice."""
import asyncio
import difflib
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from app.config import settings
from app.platform import db, current_user, decode
from app.role_fit import document, key
from app.services.cv_parser import CvParserService

router=APIRouter(prefix='/api/workbench',tags=['Evidence and interview workbench'])

def row(value):
    out=dict(value)
    for k in ('data','annotations','questions','answers','review','source','feedback'):
        if k in out:out[k]=decode(out[k])
    out.pop('run_token',None)
    return out

class SnapshotInput(BaseModel):
    kind:Literal['cv_version','evidence','repository']
    source_id:str=Field(min_length=1,max_length=200)

@router.get('/snapshots')
async def snapshots(user=Depends(current_user)):
    async with db() as c:
        return [row(r) for r in await c.fetch('SELECT * FROM evidence_snapshots WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50',user['id'])]

@router.post('/snapshots',status_code=202)
async def snapshot(body:SnapshotInput,user=Depends(current_user)):
    async with db() as c:
        source=None
        if body.kind=='cv_version':
            if not body.source_id.isdigit() or int(body.source_id)>2147483647:raise HTTPException(422,'Select a CV revision')
            found=await c.fetchrow('SELECT id,document_id,data,note,created_at FROM cv_versions WHERE id=$1 AND user_id=$2',int(body.source_id),user['id'])
            if found:source={**dict(found),'data':decode(found['data'])}
        elif body.kind=='evidence':
            found=await c.fetchrow('SELECT id,kind,title,url,detail,status,review_note,updated_at,eligibility_state,review_due_at FROM evidence WHERE id=$1 AND user_id=$2',body.source_id,user['id'])
            if found:source={**dict(found),'detail':decode(found['detail'])}
        else:
            identity=decode(await c.fetchval('SELECT github_identity FROM users WHERE id=$1',user['id'])) or {}
            found=next((r for r in identity.get('repositories',[]) if r.get('full_name')==body.source_id),None)
            if found:source={'repository':found,'synced_at':identity.get('synced_at'),'scope':'Cached owned-repository metadata; not a source-code or runtime verification'}
        if source is None:raise HTTPException(404,'Source unavailable')
        sid=secrets.token_urlsafe(18)
        await c.execute('INSERT INTO evidence_snapshots(id,user_id,kind,source_id,data) VALUES($1,$2,$3,$4,$5::jsonb)',sid,user['id'],body.kind,body.source_id,json.dumps({'source':source},default=str))
        return {'id':sid,'status':'queued'}

@router.post('/snapshots/{sid}/{action}')
async def snapshot_action(sid:str,action:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT status FROM evidence_snapshots WHERE id=$1 AND user_id=$2 FOR UPDATE',sid,user['id'])
            if not found:raise HTTPException(404,'Snapshot unavailable')
            allowed={'cancel':('queued','running'),'retry':('failed','cancelled')}
            if action not in allowed:raise HTTPException(404,'Unknown action')
            if found['status'] not in allowed[action]:raise HTTPException(409,'Snapshot state changed')
            await c.execute('UPDATE evidence_snapshots SET status=$2,error=NULL,updated_at=NOW() WHERE id=$1',sid,'cancelled' if action=='cancel' else 'queued')
    return {'ok':True}

def snapshot_text(source,content=None):
    text=CvParserService()._extract_text(content) if content is not None else json.dumps(source,indent=2,sort_keys=True,ensure_ascii=False,default=str)
    if content is not None and not text.strip():raise ValueError('No extractable PDF text')
    lines=text.splitlines()
    clipped='\n'.join(lines[:2000])[:100000]
    return {'text':clipped,'truncated':len(lines)>2000 or len('\n'.join(lines[:2000]))>100000,'text_sha256':hashlib.sha256(clipped.encode()).hexdigest(),'scope':'Extracted text only; not pixel-level document verification' if content is not None else 'Structured claim/metadata snapshot; not proof of truth',**({'file_sha256':hashlib.sha256(content).hexdigest()} if content is not None else {})}

async def snapshot_worker():
    while True:
        try:
            async with db() as c:
                async with c.transaction():
                    await c.execute("UPDATE evidence_snapshots SET status='queued' WHERE status='running' AND updated_at<NOW()-INTERVAL '2 minutes'")
                    found=await c.fetchrow("SELECT * FROM evidence_snapshots WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
                    token=secrets.token_hex(16)
                    if found:await c.execute("UPDATE evidence_snapshots SET status='running',run_token=$2,updated_at=NOW() WHERE id=$1",found['id'],token)
            if not found:
                await asyncio.sleep(.5);continue
            try:
                source=decode(found['data'])['source'];content=None
                if found['kind']=='cv_version' and source.get('document_id'):
                    async with db() as c:
                        storage=await c.fetchval('SELECT storage_key FROM documents WHERE id=$1 AND user_id=$2',source['document_id'],found['user_id'])
                    if not storage:raise ValueError('Original document unavailable')
                    root=Path(settings.storage_path).resolve();target=(root/storage).resolve()
                    if not target.is_relative_to(root):raise ValueError('Source path unavailable')
                    content=await asyncio.to_thread(target.read_bytes)
                result=await asyncio.wait_for(asyncio.to_thread(snapshot_text,source,content),30)
                async with db() as c:await c.execute("UPDATE evidence_snapshots SET status='ready',data=$3::jsonb,updated_at=NOW() WHERE id=$1 AND status='running' AND run_token=$2",found['id'],token,json.dumps({'source':source,**result},default=str))
            except Exception:
                async with db() as c:await c.execute("UPDATE evidence_snapshots SET status='failed',error='Text extraction failed or timed out. Retry or inspect the original source.',updated_at=NOW() WHERE id=$1 AND status='running' AND run_token=$2",found['id'],token)
        except asyncio.CancelledError:raise
        except Exception:await asyncio.sleep(2)

class ComparisonInput(BaseModel):
    left_id:str=Field(min_length=1,max_length=80)
    right_id:str=Field(min_length=1,max_length=80)

@router.post('/comparisons',status_code=201)
async def compare(body:ComparisonInput,user=Depends(current_user)):
    async with db() as c:
        found={r['id']:row(r) for r in await c.fetch('SELECT * FROM evidence_snapshots WHERE user_id=$1 AND id=ANY($2::text[])',user['id'],[body.left_id,body.right_id])}
        if any(sid not in found for sid in (body.left_id,body.right_id)):raise HTTPException(404,'Snapshot unavailable')
        if any(r['status']!='ready' for r in found.values()):raise HTTPException(409,'Wait for ready snapshots')
        left=found[body.left_id]['data']['text'].splitlines();right=found[body.right_id]['data']['text'].splitlines()
        ops=await asyncio.to_thread(lambda:list(difflib.SequenceMatcher(None,left,right,autojunk=True).get_opcodes()))
        cid=secrets.token_urlsafe(18)
        await c.execute('INSERT INTO evidence_comparisons(id,user_id,left_id,right_id,data) VALUES($1,$2,$3,$4,$5::jsonb)',cid,user['id'],body.left_id,body.right_id,json.dumps({'operations':ops,'scope':'Text and metadata comparison only; no automatic verification'}))
    return {'id':cid}

@router.get('/comparisons')
async def comparisons(user=Depends(current_user)):
    async with db() as c:return [row(r) for r in await c.fetch('SELECT * FROM evidence_comparisons WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30',user['id'])]

class Annotation(BaseModel):
    side:Literal['left','right']
    line:int=Field(ge=1,le=2000)
    text:str=Field(min_length=1,max_length=2000)

class AnnotationPatch(BaseModel):
    revision:int=Field(ge=0)
    annotations:list[Annotation]=Field(default_factory=list,max_length=50)

@router.put('/comparisons/{cid}/annotations')
async def annotate(cid:str,body:AnnotationPatch,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT * FROM evidence_comparisons WHERE id=$1 AND user_id=$2 FOR UPDATE',cid,user['id'])
            if not found:raise HTTPException(404,'Comparison unavailable')
            if found['revision']!=body.revision:raise HTTPException(409,'Annotations changed. Reload before replacing them')
            for note in body.annotations:
                sid=found[note.side+'_id'];data=decode(await c.fetchval('SELECT data FROM evidence_snapshots WHERE id=$1 AND user_id=$2',sid,user['id']))
                if note.line>len(data['text'].splitlines()):raise HTTPException(422,'Annotation line is outside the snapshot')
            await c.execute('UPDATE evidence_comparisons SET revision=revision+1,annotations=$2::jsonb WHERE id=$1',cid,json.dumps([a.model_dump() for a in body.annotations]))
    return {'revision':body.revision+1}

PROMPTS={
 'docker':'Explain a container failure you investigated. Cover image construction, runtime isolation and how you measured the fix.',
 'postgres':'Describe a database consistency or performance decision. Explain the transaction/query tradeoffs and measured outcome.',
 'rest-api':'Walk through an API design decision, including authorization, idempotency, errors and the evidence for your choice.',
 'jwt-auth':'Explain a token-security decision. Discuss verification, expiration, renewal and the evidence supporting the design.',
 'redis':'Describe a caching tradeoff involving TTL, consistency or stampedes. Explain how you measured the result.',
}

class PracticeInput(BaseModel):
    role_revision:int=Field(ge=0)
    requirement_indexes:list[int]=Field(default_factory=list,max_length=10)
    evidence_ids:list[str]=Field(default_factory=list,max_length=20)
    duration_seconds:int=Field(default=600,ge=60,le=3600)

@router.post('/practice',status_code=201)
async def start_practice(body:PracticeInput,user=Depends(current_user)):
    async with db() as c:
        role=await document(c,user['id']);reqs=role['requirements']
        if role['revision']!=body.role_revision:raise HTTPException(409,'Role requirements changed; refresh before starting practice')
        if not role['reviewed'] or not body.requirement_indexes:raise HTTPException(400,'Select requirements from a saved, reviewed role')
        if len(set(body.requirement_indexes))!=len(body.requirement_indexes) or any(i<0 or i>=len(reqs) for i in body.requirement_indexes):raise HTTPException(422,'Select valid unique requirements')
        evidence=[dict(r) for r in await c.fetch('SELECT id,title,kind,status FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])',user['id'],body.evidence_ids)]
        if set(body.evidence_ids)!={r['id'] for r in evidence}:raise HTTPException(404,'Practice evidence unavailable')
        questions=[{'id':str(i),'skill':reqs[i]['skill'],'prompt':PROMPTS.get(key(reqs[i]['skill']),f"Describe a concrete {reqs[i]['skill']} decision. Explain your role, the tradeoffs, outcome and supporting evidence."),'requirement':reqs[i]} for i in body.requirement_indexes]
        sid=secrets.token_urlsafe(18);now=datetime.now(timezone.utc)
        await c.execute('INSERT INTO practice_sessions(id,user_id,questions,source,expires_at) VALUES($1,$2,$3::jsonb,$4::jsonb,$5)',sid,user['id'],json.dumps(questions),json.dumps({'role':role,'evidence':evidence}),now+timedelta(seconds=body.duration_seconds))
        return {'id':sid}

def self_review(questions,answers,review):
    return {'version':'practice-checklist-v1','scope':'Deterministic self-review prompts; no badge, grade or verified-score change','items':[{'question_id':q['id'],'word_count':len(answers.get(q['id'],'').split()),'has_numeric_detail':bool(re.search(r'\d',answers.get(q['id'],''))),'checklist':review.get(q['id'],{}),'prompts':['Describe the situation and your individual action','Explain a tradeoff','State an observable result','Connect a source and describe its limits']} for q in questions]}

@router.get('/practice')
async def sessions(user=Depends(current_user)):
    async with db() as c:
        result=[row(r) for r in await c.fetch('SELECT * FROM practice_sessions WHERE user_id=$1 ORDER BY started_at DESC LIMIT 30',user['id'])]
    return {'server_now':datetime.now(timezone.utc),'sessions':result}

class PracticePatch(BaseModel):
    revision:int=Field(ge=0)
    answers:dict[str,str]=Field(default_factory=dict,max_length=10)
    review:dict[str,dict[str,bool]]=Field(default_factory=dict,max_length=10)
    finish:bool=False

    @model_validator(mode='after')
    def bounded(self):
        if any(len(v)>4000 for v in self.answers.values()):raise ValueError('Answer limit is 4000 characters')
        if any(set(v)-{'situation','tradeoff','outcome','evidence'} for v in self.review.values()):raise ValueError('Unknown self-review item')
        return self

@router.put('/practice/{sid}')
async def save_practice(sid:str,body:PracticePatch,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT * FROM practice_sessions WHERE id=$1 AND user_id=$2 FOR UPDATE',sid,user['id'])
            if not found:raise HTTPException(404,'Session unavailable')
            if found['revision']!=body.revision or found['status']=='completed':raise HTTPException(409,'Session changed or completed; reload before editing')
            questions=decode(found['questions']);ids={q['id'] for q in questions}
            if set(body.answers)-ids or set(body.review)-ids:raise HTTPException(422,'Unknown question')
            feedback=self_review(questions,body.answers,body.review)
            await c.execute('UPDATE practice_sessions SET revision=revision+1,answers=$2::jsonb,review=$3::jsonb,feedback=$4::jsonb,status=$5,updated_at=NOW() WHERE id=$1',sid,json.dumps(body.answers),json.dumps(body.review),json.dumps(feedback),'completed' if body.finish else 'active')
    return {'revision':body.revision+1,'feedback':feedback}


@router.get('/snapshots/{sid}')
async def get_snapshot(sid:str,user=Depends(current_user)):
    async with db() as c:found=await c.fetchrow('SELECT * FROM evidence_snapshots WHERE id=$1 AND user_id=$2',sid,user['id'])
    if not found:raise HTTPException(404,'Snapshot unavailable')
    return row(found)

@router.get('/comparisons/{cid}')
async def get_comparison(cid:str,user=Depends(current_user)):
    async with db() as c:found=await c.fetchrow('SELECT * FROM evidence_comparisons WHERE id=$1 AND user_id=$2',cid,user['id'])
    if not found:raise HTTPException(404,'Comparison unavailable')
    return row(found)

@router.get('/practice/{sid}')
async def get_session(sid:str,user=Depends(current_user)):
    async with db() as c:found=await c.fetchrow('SELECT * FROM practice_sessions WHERE id=$1 AND user_id=$2',sid,user['id'])
    if not found:raise HTTPException(404,'Session unavailable')
    return row(found)
