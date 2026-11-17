"""Private applications with optimistic revisions and immutable submission snapshots."""
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from app.platform import current_user, cv_data, db, decode, event
from app.role_fit import document

router = APIRouter(prefix='/api/applications', tags=['Applications'])
Stage = Literal['saved', 'applied', 'interview', 'offer', 'closed']


class Interview(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    starts_at: datetime
    duration_minutes: int = Field(default=60, ge=1, le=480)

    @model_validator(mode='after')
    def aware(self):
        if self.starts_at.tzinfo is None:
            raise ValueError('Interview time must include its timezone offset')
        return self


class Application(BaseModel):
    id: uuid.UUID
    revision: int = Field(ge=0)
    employer: str = Field(min_length=1, max_length=160)
    role: str = Field(min_length=1, max_length=160)
    stage: Stage = 'saved'
    applied_on: date | None = None
    notes: str = Field(default='', max_length=8000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    role_run_id: str | None = Field(default=None, max_length=120)
    interviews: list[Interview] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def validate_fields(self):
        self.employer = self.employer.strip()
        self.role = self.role.strip()
        if not self.employer or not self.role:
            raise ValueError('Employer and role are required')
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Choose each evidence source once')
        return self


class Revision(BaseModel):
    revision: int = Field(ge=1)


def serialize(row):
    return {**decode(row['data']), 'revision': row['revision']}


async def owned(c, uid, aid):
    row = await c.fetchrow('SELECT * FROM job_applications WHERE id=$1 AND user_id=$2', aid, uid)
    if not row:
        raise HTTPException(404, 'Application unavailable')
    return row


@router.get('')
async def listing(user=Depends(current_user)):
    async with db() as c:
        return [serialize(r) for r in await c.fetch('SELECT * FROM job_applications WHERE user_id=$1 ORDER BY updated_at DESC,id', user['id'])]


@router.put('')
async def save(body: Application, user=Depends(current_user)):
    uid, aid = user['id'], str(body.id)
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', uid)
            old = await c.fetchrow('SELECT * FROM job_applications WHERE id=$1', aid)
            if old and old['user_id'] != uid:
                raise HTTPException(404, 'Application unavailable')
            value = body.model_dump(mode='json', exclude={'revision'})
            # A lost response can be retried without duplicating the write.
            if old and old['revision'] == body.revision + 1 and decode(old['data']) == value:
                return serialize(old)
            if (old['revision'] if old else 0) != body.revision:
                raise HTTPException(409, 'Application changed. Preserve your draft and reload before reconciling.')
            ids = {r['id'] for r in await c.fetch('SELECT id FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])', uid, body.evidence_ids)}
            if ids != set(body.evidence_ids):
                raise HTTPException(404, 'Selected evidence unavailable')
            if body.role_run_id and not await c.fetchval('SELECT id FROM role_fit_runs WHERE id=$1 AND user_id=$2', body.role_run_id, uid):
                raise HTTPException(404, 'Role-fit run unavailable')
            result = await c.fetchrow('INSERT INTO job_applications(id,user_id,revision,data) VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(id) DO UPDATE SET revision=EXCLUDED.revision,data=EXCLUDED.data,updated_at=NOW() RETURNING *', aid, uid, body.revision + 1, json.dumps(value))
            await event(c, uid, 'application', f'Private application saved at revision {body.revision + 1}')
            return serialize(result)


@router.get('/{aid}/snapshots')
async def snapshots(aid: str, user=Depends(current_user)):
    async with db() as c:
        await owned(c, user['id'], aid)
        return [dict(r, data=decode(r['data'])) for r in await c.fetch('SELECT revision,data,sha256,created_at FROM application_snapshots WHERE application_id=$1 ORDER BY revision DESC', aid)]


@router.post('/{aid}/snapshots')
async def freeze(aid: str, body: Revision, user=Depends(current_user)):
    uid = user['id']
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', uid)
            app = serialize(await owned(c, uid, aid))
            if app['revision'] != body.revision:
                raise HTTPException(409, 'Application changed. Review the saved version first.')
            previous = await c.fetchrow('SELECT data,sha256 FROM application_snapshots WHERE application_id=$1 AND revision=$2', aid, body.revision)
            if previous:
                return dict(previous, data=decode(previous['data']))
            sources = [dict(r, detail=decode(r['detail'])) for r in await c.fetch('SELECT id,kind,title,url,detail,status,eligibility_state,review_due_at,updated_at FROM evidence WHERE user_id=$1 AND id=ANY($2::text[]) ORDER BY id', uid, app['evidence_ids'])]
            if len(sources) != len(app['evidence_ids']):
                raise HTTPException(409, 'Selected evidence was removed. Review your selection before snapshotting.')
            fit = await c.fetchval('SELECT result FROM role_fit_runs WHERE id=$1 AND user_id=$2', app['role_run_id'], uid) if app['role_run_id'] else None
            value = {'application': app, 'cv': await cv_data(c, uid), 'reviewed_requirements': await document(c, uid), 'role_fit': decode(fit) if fit else None, 'evidence': sources, 'scope': 'Private saved submission material. Review status is historical; this does not prove an application was sent.'}
            canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            await c.execute('INSERT INTO application_snapshots(application_id,revision,data,sha256) VALUES($1,$2,$3::jsonb,$4)', aid, body.revision, canonical, digest)
            return {'data': json.loads(canonical), 'sha256': digest}


def calendar(app):
    def escape(value):
        return value.replace('\\', '\\\\').replace('\r', '').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')
    def stamp(value):
        return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Auctor//Private applications//EN', 'CALSCALE:GREGORIAN']
    for index, item in enumerate(app['interviews']):
        start = datetime.fromisoformat(item['starts_at'].replace('Z', '+00:00'))
        lines += ['BEGIN:VEVENT', f"UID:{app['id']}-{index}@auctor.local", f"SEQUENCE:{app['revision']}", 'DTSTAMP:' + stamp(datetime.now(timezone.utc)), 'DTSTART:' + stamp(start), 'DTEND:' + stamp(start + timedelta(minutes=item['duration_minutes'])), 'SUMMARY:' + escape(item['title'] + ' — ' + app['employer']), 'END:VEVENT']
    lines.append('END:VCALENDAR')
    # Fold on UTF-8 byte boundaries as required by RFC 5545.
    folded = []
    for line in lines:
        part = ''
        for char in line:
            if len((part + char).encode('utf-8')) > 75:
                folded.append(part)
                part = ' '
            part += char
        folded.append(part)
    return '\r\n'.join(folded) + '\r\n'


@router.get('/{aid}/calendar')
async def export_calendar(aid: str, user=Depends(current_user)):
    async with db() as c:
        value = serialize(await owned(c, user['id'], aid))
    return Response(calendar(value), media_type='text/calendar', headers={'Content-Disposition': 'attachment; filename="application-interviews.ics"', 'Cache-Control': 'private, no-store'})
