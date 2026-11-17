"""Candidate case studies with reviewed, revocable publication snapshots."""
import asyncio
import copy
import html
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, model_validator

from app.platform import current_user, db, decode
from app.presentations import digest, brief_pdf

router = APIRouter(prefix='/api/case-studies', tags=['Case studies'])
SECTIONS = ('problem', 'contribution', 'approach', 'outcome', 'reflection')


class Section(BaseModel):
    kind: Literal['problem', 'contribution', 'approach', 'outcome', 'reflection']
    text: str = Field(default='', max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class Draft(BaseModel):
    revision: int = Field(default=0, ge=0)
    title: str = Field(default='My case study', min_length=1, max_length=160)
    sections: list[Section] = Field(default_factory=lambda: [Section(kind=k) for k in SECTIONS], min_length=5, max_length=5)

    @model_validator(mode='after')
    def complete_sections(self):
        if {s.kind for s in self.sections} != set(SECTIONS):
            raise ValueError('Include each case-study section exactly once')
        if not self.title.strip():
            raise ValueError('Enter a case-study title')
        return self


async def read(c, uid):
    value = await c.fetchval('SELECT data FROM case_study_drafts WHERE user_id=$1', uid)
    return decode(value) if value else Draft().model_dump()


async def projection(c, uid, draft):
    selected = {eid for section in draft['sections'] for eid in section['evidence_ids']}
    sources = {}
    for r in await c.fetch('SELECT id,title,kind,status,eligibility_state,review_due_at,updated_at FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])', uid, list(selected)):
        from app.eligibility import eligible
        if eligible(dict(r)):
            sources[r['id']] = {'id': r['id'], 'title': r['title'], 'kind': r['kind'], 'source_revision': str(r['updated_at']), 'snapshot_sha256': digest(dict(r)), 'status': 'Reviewed evidence at publication'}
    profile = await c.fetchrow('SELECT display_name,handle FROM users WHERE id=$1', uid)
    return {'title': draft['title'], 'revision': draft['revision'], 'profile': dict(profile), 'scope': 'Candidate-written narrative. Evidence review applies only to its stated scope. Metrics and outcomes are candidate claims.', 'sections': [{'kind': s['kind'], 'text': s['text'], 'evidence': [sources[eid] for eid in dict.fromkeys(s['evidence_ids']) if eid in sources]} for s in draft['sections']]}


@router.get('')
async def get_draft(user=Depends(current_user)):
    async with db() as c:
        return await read(c, user['id'])


@router.put('')
async def save(body: Draft, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', user['id'])
            previous = await read(c, user['id'])
            if body.revision != previous['revision']:
                raise HTTPException(409, 'Draft changed. Preserve your edits and reload.')
            ids = {eid for s in body.sections for eid in s.evidence_ids}
            owned = {r['id'] for r in await c.fetch('SELECT id FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])', user['id'], list(ids))}
            if ids != owned:
                raise HTTPException(404, 'Evidence unavailable')
            value = body.model_dump()
            value['revision'] += 1
            await c.execute('INSERT INTO case_study_drafts(user_id,data) VALUES($1,$2::jsonb) ON CONFLICT(user_id) DO UPDATE SET data=EXCLUDED.data', user['id'], json.dumps(value))
            await c.execute('INSERT INTO case_study_revisions(user_id,revision,data) VALUES($1,$2,$3::jsonb)', user['id'], value['revision'], json.dumps(value))
            return value


@router.get('/preview')
async def preview(user=Depends(current_user)):
    async with db() as c:
        data = await projection(c, user['id'], await read(c, user['id']))
    return {'data': data, 'sha256': digest(data)}


class Publish(BaseModel):
    revision: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    confirm_public: bool = False
    expires_days: int = Field(default=7, ge=1, le=90)


@router.post('/shares', status_code=201)
async def publish(body: Publish, user=Depends(current_user)):
    if not body.confirm_public:
        raise HTTPException(400, 'Review the public preview and confirm publication')
    async with db() as c:
        async with c.transaction(isolation='repeatable_read'):
            data = await projection(c, user['id'], await read(c, user['id']))
            if body.revision != data['revision'] or body.sha256 != digest(data):
                raise HTTPException(409, 'Sources or draft changed. Review a fresh preview.')
            sid = secrets.token_urlsafe(24)
            expires = datetime.now(timezone.utc) + timedelta(days=body.expires_days)
            await c.execute("INSERT INTO shares(id,user_id,kind,expires_at) VALUES($1,$2,'case_study',$3)", sid, user['id'], expires)
            await c.execute('INSERT INTO case_study_publications(share_id,data) VALUES($1,$2::jsonb)', sid, json.dumps(data))
    return {'id': sid, 'expires_at': expires, 'path': '/api/case-studies/shared/' + sid}


@router.get('/shares')
async def shares(user=Depends(current_user)):
    async with db() as c:
        return [dict(r) for r in await c.fetch("SELECT id,expires_at,revoked FROM shares WHERE user_id=$1 AND kind='case_study' ORDER BY created_at DESC", user['id'])]


async def public(c, sid):
    row = await c.fetchrow("SELECT s.user_id,p.data FROM shares s JOIN case_study_publications p ON p.share_id=s.id WHERE s.id=$1 AND s.kind='case_study' AND NOT s.revoked AND s.expires_at>NOW()", sid)
    if not row:
        raise HTTPException(404, 'Case study expired, revoked or unavailable')
    data = copy.deepcopy(decode(row['data']))
    current = {r['id']: str(r['updated_at']) for r in await c.fetch("SELECT id,updated_at FROM evidence WHERE user_id=$1 AND status='verified' AND eligibility_state='current' AND (review_due_at IS NULL OR review_due_at>NOW())", row['user_id'])}
    for section in data['sections']:
        original = section['evidence']
        section['evidence'] = [e for e in original if current.get(e['id']) == e['source_revision']]
        section['unavailable_sources'] = len(original) - len(section['evidence'])
    return data


def html_view(data):
    esc = html.escape
    sections = []
    for s in data['sections']:
        items = ''.join('<li>' + esc(e['title']) + ' — reviewed source revision ' + esc(e['source_revision']) + '</li>' for e in s['evidence'])
        missing = s.get('unavailable_sources', 0)
        sections.append('<section><h2>' + esc(s['kind'].title()) + '</h2><p class="narrative">' + esc(s['text']) + '</p><h3>Supporting evidence</h3>' + ('<ul>' + items + '</ul>' if items else '<p>No current reviewed source displayed.</p>') + (f'<p>{missing} historical sources are no longer available for this publication.</p>' if missing else '') + '</section>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(data['title']) + '</title><style>body{max-width:760px;margin:auto;padding:2rem;background:#faf8f2;color:#243435;font:1.1rem/1.7 system-ui}h1,h2{font-family:Georgia,serif}section{border-top:1px solid #b5a380;padding:1.5rem 0}.narrative{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#285b61}@media(prefers-color-scheme:dark){body{background:#162122;color:#f4efe5}}</style><body><main><h1>' + esc(data['title']) + '</h1><p>' + esc(data['profile']['display_name']) + '</p><p>' + esc(data['scope']) + '</p>' + ''.join(sections) + '</main></body></html>'


@router.get('/shared/{sid}')
async def view(sid: str):
    async with db() as c:
        data = await public(c, sid)
    return HTMLResponse(html_view(data), headers={'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff'})


@router.get('/shared/{sid}/report.pdf')
async def pdf(sid: str):
    async with db() as c:
        data = await public(c, sid)
    brief = {**data, 'introduction': '', 'career_story': {'milestones': []}, 'role_fit': None, 'projects': [{'name': s['kind'].title(), 'description': s['text']} for s in data['sections']], 'evidence': [e for s in data['sections'] for e in s['evidence']]}
    return Response(await asyncio.to_thread(brief_pdf, brief), media_type='application/pdf', headers={'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="auctor-case-study.pdf"'})
