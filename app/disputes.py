"""Audited appeals with independent review and revision-bound corrections."""
import json
import secrets
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.platform import current_user, db, decode, record, recalculate, event
from app.verification import evidence_hash

router = APIRouter(prefix='/api/disputes', tags=['Disputes and appeals'])
CLOSED = {'upheld', 'corrected', 'withdrawn'}


class Submission(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=10, max_length=4000)
    requested_correction: str = Field(min_length=3, max_length=2000)
    supporting_ids: list[str] = Field(default_factory=list, max_length=20)


class Action(BaseModel):
    revision: int = Field(ge=1)
    action: Literal['assign', 'under_review', 'awaiting_information', 'respond', 'upheld', 'corrected', 'withdrawn']
    note: str = Field(min_length=3, max_length=4000)
    reviewer_id: int | None = None
    corrected_status: Literal['verified', 'rejected'] | None = None
    source_sha256: str | None = None


async def independent(c, reviewer_id, owner_id, evidence_id):
    return reviewer_id != owner_id and await c.fetchval("SELECT id FROM users WHERE id=$1 AND role='reviewer'", reviewer_id) and not await c.fetchval('SELECT 1 FROM review_decisions WHERE evidence_id=$1 AND reviewer_id=$2 UNION ALL SELECT 1 FROM evidence_lifecycle_audit WHERE evidence_id=$1 AND reviewer_id=$2 LIMIT 1', evidence_id, reviewer_id)


async def audit(c, did, actor, revision, action, note, reviewer_id):
    await c.execute('INSERT INTO dispute_audit(dispute_id,actor_id,revision,action,note,reviewer_id) VALUES($1,$2,$3,$4,$5,$6)', did, actor, revision, action, note, reviewer_id)


@router.post('', status_code=201)
async def submit(body: Submission, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            source = await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND user_id=$2 FOR UPDATE', body.evidence_id, user['id'])
            if not source:
                raise HTTPException(404, 'Evidence unavailable')
            if await c.fetchval("SELECT id FROM disputes WHERE evidence_id=$1 AND state NOT IN ('upheld','corrected','withdrawn')", body.evidence_id):
                raise HTTPException(409, 'An open dispute already exists for this source')
            support = [record(r) for r in await c.fetch('SELECT * FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])', user['id'], body.supporting_ids)]
            if {r['id'] for r in support} != set(body.supporting_ids):
                raise HTTPException(404, 'Supporting evidence unavailable')
            assigned = None
            for r in await c.fetch("SELECT id FROM users WHERE role='reviewer' ORDER BY id"):
                if await independent(c, r['id'], user['id'], body.evidence_id):
                    assigned = r['id']; break
            did = secrets.token_urlsafe(18)
            data = {**body.model_dump(), 'original_source': record(source), 'supporting_snapshots': support}
            await c.execute('INSERT INTO disputes(id,user_id,evidence_id,reviewer_id,data) VALUES($1,$2,$3,$4,$5::jsonb)', did, user['id'], body.evidence_id, assigned, json.dumps(data, default=str))
            await audit(c, did, user['id'], 1, 'open', body.reason, assigned)
            await event(c, user['id'], 'dispute', 'Evidence dispute opened; verification unchanged')
            return {'id': did, 'revision': 1, 'state': 'open', 'reviewer_id': assigned}


@router.get('')
async def listing(user=Depends(current_user)):
    async with db() as c:
        return [dict(r, data=decode(r['data'])) for r in await c.fetch('SELECT * FROM disputes WHERE user_id=$1 OR $2::boolean ORDER BY updated_at DESC LIMIT 100', user['id'], user['role'] == 'reviewer')]


@router.get('/{did}')
async def detail(did: str, user=Depends(current_user)):
    async with db() as c:
        row = await c.fetchrow('SELECT * FROM disputes WHERE id=$1 AND (user_id=$2 OR $3::boolean)', did, user['id'], user['role'] == 'reviewer')
        if not row:
            raise HTTPException(404, 'Dispute unavailable')
        source = await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND user_id=$2', row['evidence_id'], row['user_id'])
        reviewers = []
        if user['role'] == 'reviewer':
            for r in await c.fetch("SELECT id,display_name FROM users WHERE role='reviewer' ORDER BY id"):
                reviewers.append({**dict(r), 'independent': bool(await independent(c, r['id'], row['user_id'], row['evidence_id']))})
        return {**dict(row), 'data': decode(row['data']), 'current_source': record(source) if source else None, 'source_sha256': evidence_hash(record(source)) if source else None, 'reviewers': reviewers, 'history': [dict(r) for r in await c.fetch('SELECT * FROM dispute_audit WHERE dispute_id=$1 ORDER BY revision,id', did)]}


@router.post('/{did}/actions')
async def transition(did: str, body: Action, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            row = await c.fetchrow('SELECT * FROM disputes WHERE id=$1 FOR UPDATE', did)
            if not row or (row['user_id'] != user['id'] and user['role'] != 'reviewer'):
                raise HTTPException(404, 'Dispute unavailable')
            if row['revision'] != body.revision or row['state'] in CLOSED:
                raise HTTPException(409, 'Dispute changed or is already closed')
            assigned, state = row['reviewer_id'], row['state']
            if body.action in ('respond', 'withdrawn'):
                if user['id'] != row['user_id']:
                    raise HTTPException(403, 'Only the candidate can respond or withdraw')
                if body.action == 'respond' and state != 'awaiting_information':
                    raise HTTPException(409, 'No information request is pending')
                state = 'under_review' if body.action == 'respond' else 'withdrawn'
            elif body.action == 'assign':
                if user['role'] != 'reviewer' or not body.reviewer_id or not await independent(c, body.reviewer_id, row['user_id'], row['evidence_id']):
                    raise HTTPException(403, 'Choose an independent reviewer; prior decision makers and the candidate are excluded')
                assigned = body.reviewer_id
            else:
                if user['role'] != 'reviewer' or assigned != user['id'] or not await independent(c, user['id'], row['user_id'], row['evidence_id']):
                    raise HTTPException(403, 'Only the assigned independent reviewer can decide')
                allowed = {'under_review': {'open', 'awaiting_information'}, 'awaiting_information': {'under_review'}, 'upheld': {'under_review'}, 'corrected': {'under_review'}}
                if state not in allowed[body.action]:
                    raise HTTPException(409, 'Invalid dispute transition')
                state = body.action
                if state == 'corrected':
                    source = await c.fetchrow('SELECT * FROM evidence WHERE id=$1 AND user_id=$2 FOR UPDATE', row['evidence_id'], row['user_id'])
                    if not source or not body.corrected_status or body.source_sha256 != evidence_hash(record(source)):
                        raise HTTPException(409, 'Source changed or correction missing. Inspect the current evidence again.')
                    if body.corrected_status == 'verified' and not source['url'] and not source['storage_key']:
                        raise HTTPException(422, 'Verification requires a source URL or proof file')
                    due = source['review_due_at']
                    eligibility = 'revoked' if body.corrected_status == 'rejected' else ('stale' if due and due <= datetime.now(timezone.utc) else 'current')
                    await c.execute('UPDATE evidence SET status=$2,eligibility_state=$3,reviewer_id=$4,review_note=$5,updated_at=NOW() WHERE id=$1', row['evidence_id'], body.corrected_status, eligibility, user['id'], body.note)
                    await c.execute('INSERT INTO review_decisions(evidence_id,user_id,reviewer_id,title,kind,status,note,source) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)', row['evidence_id'], row['user_id'], user['id'], source['title'], source['kind'], body.corrected_status, body.note, json.dumps({'dispute_id': did, 'prior_source': record(source)}, default=str))
                    await c.execute('INSERT INTO evidence_lifecycle_audit(evidence_id,user_id,reviewer_id,state,note,review_due_at) VALUES($1,$2,$3,$4,$5,$6)', row['evidence_id'], row['user_id'], user['id'], eligibility, body.note, due)
                    await c.execute('UPDATE verification_receipts SET revoked=TRUE WHERE user_id=$1 AND evidence_id=$2', row['user_id'], row['evidence_id'])
                    await recalculate(c, row['user_id'], 'Evidence corrected after appeal')
            revision = row['revision'] + 1
            await c.execute('UPDATE disputes SET state=$2,reviewer_id=$3,revision=$4,updated_at=NOW() WHERE id=$1', did, state, assigned, revision)
            await audit(c, did, user['id'], revision, body.action, body.note, assigned)
            await event(c, row['user_id'], 'dispute', 'Dispute updated: ' + state)
            return {'id': did, 'revision': revision, 'state': state, 'reviewer_id': assigned}
