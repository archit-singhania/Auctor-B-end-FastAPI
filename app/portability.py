"""Durable, owned account archives with bounded output and export retention."""
import asyncio
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.platform import current_user, db, decode
from app.config import settings

router = APIRouter(prefix='/api/portability', tags=['Account portability'])
TABLES = ('users','cv_data','verifications','badges','scores','portability_preferences','documents','cv_versions','evidence','attempts','score_history','review_decisions','activity','shares','saved_candidates','career_stories','role_documents','role_fit_runs','evidence_snapshots','evidence_comparisons','practice_sessions','recruiter_presentations','verification_receipts','evidence_lifecycle_audit','evidence_rechecks','assessment_integrity_events','assessment_integrity_signals','recruiter_integrations','collection_links','job_applications','case_study_drafts','case_study_revisions','analytics_settings','analytics_daily','recruiter_exploration','evidence_lineage_snapshots','disputes','organization_members')
SECRET_FIELDS = {'password_hash','token_hash','secret_cipher','storage_key','run_token','question_snapshot','canonical_text'}
LIMIT = 256 * 1024 * 1024


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k,v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def safe_path(key):
    root = Path(settings.storage_path).resolve()
    candidate = root / key
    if candidate.is_absolute() and not candidate.resolve().is_relative_to(root):
        raise ValueError('Archive path escapes storage')
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('Symlinked storage is not exportable')
        if cursor == cursor.parent:
            raise ValueError('Invalid storage path')
        cursor = cursor.parent
    return candidate


async def export_data(c, uid):
    data = {}
    files = []
    serialized_size = 0
    async with c.transaction(isolation='repeatable_read', readonly=True):
        available = {r['table_name'] for r in await c.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")}
        for table in TABLES:
            if table not in available:
                continue
            column = 'id' if table == 'users' else 'user_id'
            rows = c.cursor('SELECT * FROM ' + table + ' WHERE ' + column + '=$1', uid, prefetch=20)
            values = []
            async for row in rows:
                item = dict(row)
                # JSONB is decoded explicitly; arbitrary text columns remain text.
                for key, value in item.items():
                    if isinstance(value, str) and value[:1] in ('{','['):
                        try: item[key] = json.loads(value)
                        except ValueError: pass
                if table in ('documents','evidence') and item.get('storage_key'):
                    files.append((table, str(item['id']), item['storage_key']))
                item = redact(item)
                serialized_size += len(json.dumps(item, default=str).encode())
                if serialized_size > LIMIT:
                    raise ValueError('Account records exceed export budget')
                values.append(item)
            data[table] = values
        joins = {
            'integrity_review_audit': 'SELECT a.* FROM integrity_review_audit a JOIN assessment_integrity_signals s ON s.id=a.signal_id WHERE s.user_id=$1',
            'integration_outbox': 'SELECT a.* FROM integration_outbox a JOIN recruiter_integrations i ON i.id=a.integration_id WHERE i.user_id=$1',
            'case_study_publications': 'SELECT p.* FROM case_study_publications p JOIN shares s ON s.id=p.share_id WHERE s.user_id=$1',
            'recruiter_presentation_shares': 'SELECT p.* FROM recruiter_presentation_shares p JOIN shares s ON s.id=p.share_id WHERE s.user_id=$1',
            'application_snapshots': 'SELECT s.* FROM application_snapshots s JOIN job_applications a ON a.id=s.application_id WHERE a.user_id=$1',
            'dispute_audit': 'SELECT a.* FROM dispute_audit a JOIN disputes d ON d.id=a.dispute_id WHERE d.user_id=$1',
        }
        for name, query in joins.items():
            if name in available:
                data[name] = []
                async for r in c.cursor(query, uid, prefetch=20):
                    item = redact({k: decode(v) if isinstance(v,str) and v[:1] in ('{','[') else v for k,v in dict(r).items()})
                    serialized_size += len(json.dumps(item, default=str).encode())
                    if serialized_size > LIMIT:
                        raise ValueError('Account records exceed export budget')
                    data[name].append(item)
    return data, files


def archive(jid, data, files):
    destination = safe_path('exports/' + jid + '.zip')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.partial')
    manifest = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'files': [], 'scope': 'Owned application records and referenced source files. Credentials, assessment keys and organization-owned notes are excluded. This is a portability archive, not a database recovery backup.'}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    size = len(raw)
    if size > LIMIT:
        raise ValueError('Account archive exceeds the supported size')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('records.json', raw)
            for index, (table, rid, key) in enumerate(files):
                path = safe_path(key)
                length = path.stat().st_size
                if length > 10*1024*1024 or size + length > LIMIT:
                    raise ValueError('Account files exceed the supported size')
                content = path.read_bytes()
                if len(content) != length:
                    raise ValueError('A source changed during export; retry')
                size += length
                name = f'files/{index:05d}.pdf'
                output.writestr(name, content)
                manifest['files'].append({'record_type': table, 'record_id': rid, 'archive_path': name, 'bytes': length, 'sha256': hashlib.sha256(content).hexdigest()})
            manifest['records_sha256'] = hashlib.sha256(raw).hexdigest()
            output.writestr('manifest.json', json.dumps(manifest, indent=2))
        temporary.replace(destination)
        return str(destination), hashlib.sha256(destination.read_bytes()).hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


class Retention(BaseModel):
    export_days: int = Field(ge=1, le=30)


@router.get('')
async def state(user=Depends(current_user)):
    async with db() as c:
        days = await c.fetchval('SELECT export_days FROM portability_preferences WHERE user_id=$1', user['id']) or 7
        jobs = [dict(r) for r in await c.fetch('SELECT id,status,error,sha256,created_at,expires_at FROM portability_jobs WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50', user['id'])]
    return {'export_days': days, 'jobs': jobs, 'scope': 'Retention applies to generated export archives only. Original account records are unchanged. Account deletion uses a separate reviewed workflow. Backup expiry remains an operator responsibility.'}


@router.put('/retention')
async def retention(body: Retention, user=Depends(current_user)):
    async with db() as c:
        await c.execute('INSERT INTO portability_preferences(user_id,export_days) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET export_days=EXCLUDED.export_days', user['id'], body.export_days)
    return {'export_days': body.export_days, 'scope': 'Applies to future exports'}


@router.post('/exports', status_code=202)
async def create(user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', user['id'])
            active = await c.fetchval("SELECT id FROM portability_jobs WHERE user_id=$1 AND status IN ('queued','running')", user['id'])
            if active:
                return {'id': active, 'replayed': True}
            jid = secrets.token_urlsafe(18)
            days = await c.fetchval('SELECT export_days FROM portability_preferences WHERE user_id=$1', user['id']) or 7
            await c.execute("INSERT INTO portability_jobs(id,user_id,expires_at) VALUES($1,$2,NOW()+($3*INTERVAL '1 day'))", jid, user['id'], days)
    return {'id': jid, 'replayed': False}


@router.post('/exports/{jid}/retry')
async def retry(jid: str, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', user['id'])
            if await c.fetchval("SELECT id FROM portability_jobs WHERE user_id=$1 AND status IN ('queued','running')", user['id']):
                raise HTTPException(409, 'An export is already queued or running')
            result = await c.execute("UPDATE portability_jobs SET status='queued',error=NULL WHERE id=$1 AND user_id=$2 AND status='failed' AND expires_at>NOW()", jid, user['id'])
            if result == 'UPDATE 0':
                raise HTTPException(404, 'Retry unavailable or export expired')
    return {'status': 'queued'}


@router.get('/exports/{jid}/download')
async def download(jid: str, user=Depends(current_user)):
    async with db() as c:
        job = await c.fetchrow("SELECT id FROM portability_jobs WHERE id=$1 AND user_id=$2 AND status='ready' AND expires_at>NOW()", jid, user['id'])
    if not job:
        raise HTTPException(404, 'Archive unavailable or expired')
    path = safe_path('exports/' + job['id'] + '.zip')
    if not path.is_file():
        raise HTTPException(409, 'Archive file missing; create another export')
    return FileResponse(path, media_type='application/zip', filename='auctor-account.zip', headers={'Cache-Control': 'no-store'})


async def work_once():
    async with db() as c:
        # Session advisory lock is released on worker/process failure, allowing recovery.
        locked = await c.fetchval('SELECT pg_try_advisory_lock(73722903)')
        if not locked:
            return
        try:
            for expired in await c.fetch("SELECT id FROM portability_jobs WHERE expires_at<=NOW() AND status!='expired'"):
                safe_path('exports/' + expired['id'] + '.zip').unlink(missing_ok=True)
                safe_path('exports/' + expired['id'] + '.partial').unlink(missing_ok=True)
                await c.execute("UPDATE portability_jobs SET status='expired',error=NULL WHERE id=$1", expired['id'])
            await c.execute("UPDATE portability_jobs SET status='queued' WHERE status='running'")
            job = await c.fetchrow("UPDATE portability_jobs SET status='running' WHERE id=(SELECT id FROM portability_jobs WHERE status='queued' AND expires_at>NOW() ORDER BY created_at LIMIT 1) RETURNING *")
            if not job:
                return
            try:
                data, files = await export_data(c, job['user_id'])
                task = asyncio.create_task(asyncio.to_thread(archive, job['id'], data, files))
                try:
                    _, sha = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Do not release the lease while a filesystem thread still writes.
                    with suppress(Exception):
                        await task
                    raise
                await c.execute("UPDATE portability_jobs SET status='ready',sha256=$2,error=NULL WHERE id=$1", job['id'], sha)
            except Exception:
                await c.execute("UPDATE portability_jobs SET status='failed',error='Export failed. A source may be missing, unsafe, changed or above the 256 MiB limit. Retry after inspecting the account files.' WHERE id=$1", job['id'])
        finally:
            await c.execute('SELECT pg_advisory_unlock(73722903)')


async def worker():
    while True:
        try:
            await work_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(1)
