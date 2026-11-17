"""Opt-in aggregate analytics without visitor identifiers or network metadata."""
import hashlib
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from app.platform import current_user, db
from app.domain import token_hash

router = APIRouter(prefix='/api/analytics', tags=['Profile analytics'])


class Consent(BaseModel):
    enabled: bool


class Event(BaseModel):
    context: Literal['public', 'share']
    identifier: str = Field(min_length=1, max_length=120)
    kind: Literal['profile_view', 'evidence_open']
    evidence_id: str | None = Field(default=None, max_length=120)


@router.get('')
async def report(user=Depends(current_user)):
    async with db() as c:
        enabled = await c.fetchval('SELECT enabled FROM analytics_settings WHERE user_id=$1', user['id'])
        days = [dict(r) for r in await c.fetch('SELECT day,kind,count FROM analytics_daily WHERE user_id=$1 ORDER BY day DESC,kind LIMIT 1098', user['id'])]
    return {'enabled': bool(enabled), 'days': days, 'scope': 'Approximate interactions, not unique people. Repeated requests for the same resource within ten seconds are merged across all visitors. Authenticated owner previews are excluded. No IP addresses, fingerprints or visitor IDs are stored. Counts can include bots and direct API calls. Private profiles are excluded even when shared.'}


@router.put('')
async def consent(body: Consent, user=Depends(current_user)):
    async with db() as c:
        await c.execute('INSERT INTO analytics_settings(user_id,enabled) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled', user['id'], body.enabled)
    return {'enabled': body.enabled}


@router.delete('')
async def reset(user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT user_id FROM analytics_settings WHERE user_id=$1 FOR UPDATE', user['id'])
            await c.execute('DELETE FROM analytics_daily WHERE user_id=$1', user['id'])
            await c.execute('DELETE FROM analytics_bursts WHERE user_id=$1', user['id'])
    return {'reset': True}


async def target(c, body):
    if body.context == 'public':
        uid = await c.fetchval('SELECT id FROM users WHERE handle=$1 AND discoverable=TRUE', body.identifier)
    else:
        uid = await c.fetchval("SELECT user_id FROM shares WHERE id=$1 AND kind='profile' AND NOT revoked AND (expires_at IS NULL OR expires_at>NOW())", body.identifier)
    if not uid:
        raise HTTPException(404, 'Profile or share unavailable')
    source = None
    if body.kind == 'evidence_open':
        source = await c.fetchrow("SELECT url FROM evidence WHERE id=$1 AND user_id=$2 AND status='verified' AND eligibility_state='current' AND (review_due_at IS NULL OR review_due_at>NOW())", body.evidence_id, uid)
        if not source:
            raise HTTPException(404, 'Evidence unavailable')
    return uid, source


@router.post('/events')
async def record(body: Event, request: Request):
    async with db() as c:
        async with c.transaction():
            uid, source = await target(c, body)
            enabled = await c.fetchval('SELECT enabled FROM analytics_settings WHERE user_id=$1 FOR UPDATE', uid)
            authorization = request.headers.get('authorization', '')
            visitor = None
            if authorization.startswith('Bearer '):
                visitor = await c.fetchval('SELECT user_id FROM sessions WHERE token_hash=$1 AND expires_at>NOW()', token_hash(authorization[7:]))
            visible = await c.fetchval('SELECT discoverable FROM users WHERE id=$1', uid)
            counted = False
            if enabled and visible and visitor != uid:
                # This key identifies a content resource, never a visitor.
                resource = hashlib.sha256(f'{body.context}:{body.identifier}:{body.kind}:{body.evidence_id or ""}'.encode()).hexdigest()
                await c.execute("DELETE FROM analytics_bursts WHERE user_id=$1 AND last_counted<NOW()-INTERVAL '10 seconds'", uid)
                inserted = await c.fetchval('INSERT INTO analytics_bursts(user_id,resource) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING resource', uid, resource)
                if inserted:
                    kinds = [body.kind] + (['share_activity'] if body.context == 'share' else [])
                    for kind in kinds:
                        await c.execute("INSERT INTO analytics_daily(user_id,day,kind,count) VALUES($1,(NOW() AT TIME ZONE 'UTC')::date,$2,1) ON CONFLICT(user_id,day,kind) DO UPDATE SET count=analytics_daily.count+1", uid, kind)
                    counted = True
            return {'counted': counted, **({'url': source['url']} if source else {})}
