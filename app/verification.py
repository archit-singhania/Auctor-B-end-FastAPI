"""Signed scope receipts, evidence review lifecycle and human-reviewed integrity signals."""
import asyncio
import base64
import hashlib
import json
import secrets
from datetime import datetime,timezone
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,field_validator
from app.platform import db,current_user,reviewer,event,record,recalculate,decode
from app.eligibility import eligible
from app import verification_keys as keys

router=APIRouter(prefix='/api/verification',tags=['Verification provenance'])

@router.get('/evidence')
async def evidence_list(user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch('SELECT e.*,u.handle FROM evidence e JOIN users u ON u.id=e.user_id WHERE ($1::boolean OR e.user_id=$2) ORDER BY e.updated_at DESC LIMIT 100',user['role']=='reviewer',user['id'])
        return [record(r) for r in rows]

@router.get('/evidence/{eid}')
async def evidence_detail(eid:str,user=Depends(current_user)):
    async with db() as c:found=await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND ($2::boolean OR user_id=$3)',eid,user['role']=='reviewer',user['id'])
    if not found:raise HTTPException(404,'Evidence unavailable')
    return record(found)

@router.get('/lifecycle-audit')
async def lifecycle_audit(user=Depends(current_user)):
    async with db() as c:return [dict(r) for r in await c.fetch('SELECT * FROM evidence_lifecycle_audit WHERE ($1::boolean OR user_id=$2) ORDER BY id DESC LIMIT 100',user['role']=='reviewer',user['id'])]

@router.get('/integrity-audit')
async def integrity_audit(user=Depends(current_user)):
    async with db() as c:
        return [dict(r) for r in await c.fetch('SELECT a.* FROM integrity_review_audit a JOIN assessment_integrity_signals s ON s.id=a.signal_id WHERE ($1::boolean OR s.user_id=$2) ORDER BY a.id DESC LIMIT 100',user['role']=='reviewer',user['id'])]

def evidence_hash(evidence):
    snapshot={k:evidence.get(k) for k in ('id','kind','title','url','detail','review_note','status','updated_at','eligibility_state','review_due_at')}
    for field in ('updated_at','review_due_at'):
        if isinstance(snapshot[field],datetime):snapshot[field]=snapshot[field].isoformat()
    return hashlib.sha256(keys.canonical(snapshot)).hexdigest()

def receipt_status(receipt,evidence):
    valid=keys.verify(receipt['payload'],receipt['signature'])
    return {'signature_valid':valid,'revoked':receipt['revoked'],'currently_eligible':bool(evidence and eligible(evidence)),'snapshot_current':bool(evidence and evidence_hash(evidence)==receipt['payload']['evidence_snapshot_hash']),'scope_notice':'A signature proves the issuer signed this snapshot hash and scope. It does not establish the truth of a claim.'}

@router.get('/keys')
async def public_keys():return {'keys':keys.public_keys()}

@router.get('/receipts')
async def receipts(user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch('SELECT * FROM verification_receipts WHERE user_id=$1 ORDER BY created_at DESC LIMIT 100',user['id'])
        return [{**dict(r),'payload':json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload']} for r in rows]

@router.post('/evidence/{eid}/receipts',status_code=201)
async def issue(eid:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            evidence=await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND user_id=$2 FOR UPDATE',eid,user['id'])
            if not evidence:raise HTTPException(404,'Evidence unavailable')
            evidence=record(evidence)
            if not eligible(evidence):raise HTTPException(409,'Only currently eligible verified evidence can receive a receipt')
            await c.execute('SELECT pg_advisory_xact_lock(73722901)')
            kid,key=keys.signing_key();rid=secrets.token_urlsafe(24)
            scope='Repository ownership/provenance snapshot; declared skills remain unassessed' if evidence['kind'] in ('github','project') else 'Reviewer-approved evidence snapshot; not an issuer or employment guarantee'
            payload={'version':1,'receipt_id':rid,'evidence_id':eid,'evidence_snapshot_hash':evidence_hash(evidence),'scope':scope,'issuer':'Auctor verification service','issued_at':datetime.now(timezone.utc).isoformat(),'key_id':kid}
            signature=base64.b64encode(key.sign(keys.canonical(payload))).decode()
            await c.execute('INSERT INTO verification_receipts(id,user_id,evidence_id,payload,signature) VALUES($1,$2,$3,$4::jsonb,$5)',rid,user['id'],eid,json.dumps(payload),signature)
            await event(c,user['id'],'verification','A scoped verification receipt was issued')
    return {'payload':payload,'signature':signature,'status':receipt_status({'payload':payload,'signature':signature,'revoked':False},evidence)}

class SignedReceipt(BaseModel):
    payload:dict=Field(max_length=12)
    signature:str=Field(max_length=200)

@router.post('/verify')
async def verify(body:SignedReceipt):
    if len(keys.canonical(body.payload))>5000:raise HTTPException(413,'Receipt too large')
    valid=keys.verify(body.payload,body.signature)
    if not valid:return {'signature_valid':False,'scope_notice':'Signature invalid; do not rely on this receipt'}
    async with db() as c:
        stored=await c.fetchrow('SELECT * FROM verification_receipts WHERE id=$1',body.payload.get('receipt_id'))
        evidence=await c.fetchrow('SELECT * FROM evidence WHERE id=$1',body.payload.get('evidence_id'))
    expected=record(stored) if stored else None
    if expected and keys.canonical(decode(expected['payload']))!=keys.canonical(body.payload):expected=None
    return receipt_status({'payload':body.payload,'signature':body.signature,'revoked':not expected or expected['revoked']},record(evidence) if evidence else None)

@router.get('/receipts/{rid}')
async def public_receipt(rid:str):
    async with db() as c:
        found=await c.fetchrow('SELECT * FROM verification_receipts WHERE id=$1',rid)
        if not found:raise HTTPException(404,'Receipt unavailable')
        from app.platform import decode
        value={**dict(found),'payload':decode(found['payload'])}
        evidence=await c.fetchrow('SELECT * FROM evidence WHERE id=$1',found['evidence_id'])
        return {'payload':value['payload'],'signature':value['signature'],'status':receipt_status(value,record(evidence) if evidence else None)}

@router.delete('/receipts/{rid}')
async def revoke(rid:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            result=await c.execute('UPDATE verification_receipts SET revoked=TRUE WHERE id=$1 AND user_id=$2',rid,user['id'])
            if result=='UPDATE 0':raise HTTPException(404,'Receipt unavailable')
            await event(c,user['id'],'verification','A verification receipt was revoked')
    return {'ok':True}

class Lifecycle(BaseModel):
    state:Literal['current','stale','revoked']
    review_due_at:datetime|None=None
    note:str=Field(min_length=3,max_length=2000)
    @field_validator('review_due_at')
    @classmethod
    def timezone_required(cls,value):
        if value and value.tzinfo is None:raise ValueError('Include the timezone in the review date')
        return value

@router.put('/evidence/{eid}/lifecycle')
async def lifecycle(eid:str,body:Lifecycle,user=Depends(reviewer)):
    async with db() as c:
        async with c.transaction():
            evidence=await c.fetchrow('SELECT * FROM evidence WHERE id=$1 FOR UPDATE',eid)
            if not evidence:raise HTTPException(404,'Evidence unavailable')
            if evidence['user_id']==user['id']:raise HTTPException(403,'Independent review is required')
            if body.state=='current' and evidence['status']!='verified':raise HTTPException(409,'Review the underlying evidence before making it eligible')
            await c.execute('UPDATE evidence SET eligibility_state=$2,review_due_at=$3,updated_at=NOW() WHERE id=$1',eid,body.state,body.review_due_at)
            await c.execute('INSERT INTO evidence_lifecycle_audit(evidence_id,user_id,reviewer_id,state,note,review_due_at) VALUES($1,$2,$3,$4,$5,$6)',eid,evidence['user_id'],user['id'],body.state,body.note,body.review_due_at)
            await recalculate(c,evidence['user_id'],'Evidence eligibility reviewed')
            await event(c,evidence['user_id'],'verification','Evidence eligibility updated by a reviewer')
    return {'ok':True}

@router.post('/evidence/{eid}/recheck',status_code=201)
async def recheck(eid:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT id FROM evidence WHERE id=$1 AND user_id=$2 FOR UPDATE',eid,user['id'])
            if not found:raise HTTPException(404,'Evidence unavailable')
            existing=await c.fetchval("SELECT id FROM evidence_rechecks WHERE evidence_id=$1 AND status='pending'",eid)
            if existing:return {'id':existing,'replayed':True}
            rid=secrets.token_urlsafe(18)
            await c.execute('INSERT INTO evidence_rechecks(id,evidence_id,user_id,reason) VALUES($1,$2,$3,$4)',rid,eid,user['id'],'Owner requested manual re-verification')
            return {'id':rid,'replayed':False}

@router.get('/rechecks')
async def checks(user=Depends(current_user)):
    async with db() as c:
        if user['role']=='reviewer':rows=await c.fetch("SELECT r.*,e.title FROM evidence_rechecks r JOIN evidence e ON e.id=r.evidence_id ORDER BY r.created_at DESC LIMIT 100")
        else:rows=await c.fetch('SELECT r.*,e.title FROM evidence_rechecks r JOIN evidence e ON e.id=r.evidence_id WHERE r.user_id=$1 ORDER BY r.created_at DESC LIMIT 100',user['id'])
        return [dict(r) for r in rows]

class RecheckDecision(Lifecycle):
    approved:bool

@router.post('/rechecks/{rid}/decision')
async def decision(rid:str,body:RecheckDecision,user=Depends(reviewer)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT * FROM evidence_rechecks WHERE id=$1 FOR UPDATE',rid)
            if not found:raise HTTPException(404,'Review unavailable')
            if found['status']!='pending':raise HTTPException(409,'Re-verification already decided')
            if found['user_id']==user['id']:raise HTTPException(403,'Independent review is required')
            if body.approved and await c.fetchval('SELECT status FROM evidence WHERE id=$1',found['evidence_id'])!='verified':raise HTTPException(409,'Review the underlying proof before approving re-verification')
            await c.execute('UPDATE evidence_rechecks SET status=$2,reviewer_id=$3,note=$4,decided_at=NOW() WHERE id=$1',rid,'approved' if body.approved else 'rejected',user['id'],body.note)
            state=body.state if body.approved else 'revoked'
            await c.execute('UPDATE evidence SET eligibility_state=$2,review_due_at=$3,updated_at=NOW() WHERE id=$1',found['evidence_id'],state,body.review_due_at)
            await c.execute('INSERT INTO evidence_lifecycle_audit(evidence_id,user_id,reviewer_id,state,note,review_due_at) VALUES($1,$2,$3,$4,$5,$6)',found['evidence_id'],found['user_id'],user['id'],state,body.note,body.review_due_at)
            await recalculate(c,found['user_id'],'Evidence re-verification decision')
            await event(c,found['user_id'],'verification','A reviewer decided the re-verification request')
    return {'ok':True}

async def expire_due():
    async with db() as c:
        async with c.transaction():
            rows=await c.fetch("SELECT * FROM evidence WHERE status='verified' AND eligibility_state='current' AND review_due_at<=NOW() FOR UPDATE SKIP LOCKED LIMIT 100")
            for evidence in rows:
                await c.execute("UPDATE evidence SET eligibility_state='stale',updated_at=NOW() WHERE id=$1",evidence['id'])
                await c.execute('INSERT INTO evidence_lifecycle_audit(evidence_id,user_id,state,note,review_due_at) VALUES($1,$2,$3,$4,$5)',evidence['id'],evidence['user_id'],'stale','Scheduled review date reached',evidence['review_due_at'])
                if not await c.fetchval("SELECT id FROM evidence_rechecks WHERE evidence_id=$1 AND status='pending'",evidence['id']):await c.execute('INSERT INTO evidence_rechecks(id,evidence_id,user_id,reason) VALUES($1,$2,$3,$4)',secrets.token_urlsafe(18),evidence['id'],evidence['user_id'],'Scheduled review date reached; reviewer action required')
                await event(c,evidence['user_id'],'verification','An evidence review date was reached; re-verification is required')
                await recalculate(c,evidence['user_id'],'Evidence review date reached')
    return len(rows)

async def lifecycle_worker():
    while True:
        try:await expire_due()
        except asyncio.CancelledError:raise
        except Exception:pass
        await asyncio.sleep(5)

async def integrity_event(c,attempt,outcome):
    seq=await c.fetchval('SELECT COALESCE(MAX(sequence),0)+1 FROM assessment_integrity_events WHERE attempt_id=$1',attempt['id'])
    elapsed=max(0,(datetime.now(timezone.utc)-attempt['started_at']).total_seconds())
    await c.execute('INSERT INTO assessment_integrity_events(user_id,attempt_id,sequence,outcome,elapsed_seconds) VALUES($1,$2,$3,$4,$5)',attempt['user_id'],attempt['id'],seq,outcome,elapsed)
    recent=await c.fetchval("SELECT COUNT(*) FROM attempts WHERE user_id=$1 AND started_at>NOW()-INTERVAL '1 hour'",attempt['user_id'])
    rules=[]
    if outcome=='submitted' and elapsed<3:rules.append(('rapid_submission',{'elapsed_seconds':round(elapsed,3),'threshold_seconds':3,'explanation':'Very short server-observed duration; requires human context, not evidence of cheating'}))
    if recent>=5:rules.append(('repeated_attempts',{'attempts_last_hour':recent,'threshold':5,'explanation':'Repeated attempt pattern; review context, do not automatically reject'}))
    if outcome=='replayed' and seq>=4:rules.append(('repeated_replays',{'sequence':seq,'explanation':'Repeated replay requests can be normal network retries; human review required'}))
    for kind,data in rules:
        await c.execute("INSERT INTO assessment_integrity_signals(id,user_id,attempt_id,kind,data) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(attempt_id,kind) DO NOTHING",secrets.token_urlsafe(18),attempt['user_id'],attempt['id'],kind,json.dumps({'version':'integrity-v1',**data}))

@router.get('/integrity')
async def signals(user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch('SELECT * FROM assessment_integrity_signals WHERE ($1::boolean OR user_id=$2) ORDER BY created_at DESC LIMIT 100',user['role']=='reviewer',user['id'])
        from app.platform import decode
        return [{**dict(r),'data':decode(r['data'])} for r in rows]

class IntegrityReview(BaseModel):
    disposition:Literal['reviewed','dismissed','needs_followup']
    note:str=Field(min_length=3,max_length=2000)

@router.post('/integrity/{sid}/review')
async def review_signal(sid:str,body:IntegrityReview,user=Depends(reviewer)):
    async with db() as c:
        async with c.transaction():
            found=await c.fetchrow('SELECT * FROM assessment_integrity_signals WHERE id=$1 FOR UPDATE',sid)
            if not found:raise HTTPException(404,'Signal unavailable')
            if found['user_id']==user['id']:raise HTTPException(403,'Independent review is required')
            await c.execute('INSERT INTO integrity_review_audit(signal_id,reviewer_id,disposition,note) VALUES($1,$2,$3,$4)',sid,user['id'],body.disposition,body.note)
            await c.execute('UPDATE assessment_integrity_signals SET review_status=$2 WHERE id=$1',sid,body.disposition)
            await event(c,found['user_id'],'integrity','An integrity signal received a human disposition; grading remains unchanged')
    return {'ok':True}


@router.get('/attempts/{aid}/events')
async def attempt_events(aid:str,user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch('SELECT sequence,outcome,elapsed_seconds,received_at FROM assessment_integrity_events WHERE attempt_id=$1 AND ($2::boolean OR user_id=$3) ORDER BY sequence LIMIT 100',aid,user['role']=='reviewer',user['id'])
        if not rows:raise HTTPException(404,'Attempt observations unavailable')
        return [dict(r) for r in rows]
