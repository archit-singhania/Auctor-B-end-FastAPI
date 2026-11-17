"""Reviewed account deletion with an external, restore-time tombstone ledger."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.config import settings
from app.domain import check_password, token_hash
from app.platform import current_user, db, decode
from app.portability import safe_path

router = APIRouter(prefix='/api/account-deletion', tags=['Account deletion'])


def ledger_root():
    storage = Path(settings.storage_path).resolve()
    return Path(settings.deletion_ledger_path).resolve() if settings.deletion_ledger_path else storage.parent / (storage.name + '-deletion-ledger')


def write_tombstone(key):
    if not re.fullmatch('[a-f0-9]{32}', key):
        raise ValueError('Invalid account key')
    root = ledger_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / (key + '.json')
    if target.exists():
        return
    temporary = root / (key + '.partial')
    temporary.write_text(json.dumps({'version': 1, 'account_key': key, 'requested_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
    temporary.replace(target)


async def preview_data(c, uid):
    user = await c.fetchrow('SELECT handle,account_key FROM users WHERE id=$1', uid)
    counts = {}
    tables = await c.fetch("SELECT table_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_name='user_id' ORDER BY table_name")
    for row in tables:
        table = row['table_name']
        if not re.fullmatch('[a-z_]+', table) or table == 'account_deletions':
            continue
        counts[table] = await c.fetchval('SELECT COUNT(*) FROM ' + table + ' WHERE user_id=$1', uid)
    owners = [dict(r) for r in await c.fetch("SELECT o.id,o.name FROM organizations o JOIN organization_members m ON m.organization_id=o.id WHERE m.user_id=$1 AND m.role='owner'", uid)]
    data = {'handle': user['handle'], 'counts': counts, 'owned_organizations': owners, 'scope': 'Purges owned active-store records and attributable files. Reviews belonging to other accounts remain with reviewer identity removed. Retained backups expire under the operator backup policy; restoration must replay the external tombstone ledger.', 'backup_policy_days': settings.backup_retention_days}
    digest = hashlib.sha256(json.dumps({'account_key': user['account_key'], **data}, sort_keys=True).encode()).hexdigest()
    return {**data, 'preview_sha256': digest}


async def disable(c, uid):
    await c.execute("SELECT set_config('app.privacy_purge','on',true)")
    await c.execute("UPDATE users SET account_state='deleting',discoverable=FALSE WHERE id=$1", uid)
    await c.execute('DELETE FROM sessions WHERE user_id=$1', uid)
    await c.execute('DELETE FROM oauth_states WHERE user_id=$1', uid)
    await c.execute('UPDATE shares SET revoked=TRUE WHERE user_id=$1', uid)
    await c.execute('UPDATE recruiter_integrations SET revoked=TRUE WHERE user_id=$1', uid)
    await c.execute('UPDATE verification_receipts SET revoked=TRUE WHERE user_id=$1', uid)
    await c.execute("UPDATE evidence SET eligibility_state='revoked' WHERE user_id=$1", uid)
    await c.execute("UPDATE documents SET status='cancelled',run_token=NULL WHERE user_id=$1 AND status IN ('queued','running')", uid)
    await c.execute("UPDATE evidence_snapshots SET status='cancelled',run_token=NULL WHERE user_id=$1 AND status IN ('queued','running')", uid)
    await c.execute("UPDATE organization_invites SET revoked=TRUE WHERE email=(SELECT LOWER(email) FROM users WHERE id=$1)", uid)


@router.get('/preview')
async def preview(user=Depends(current_user)):
    async with db() as c:
        return await preview_data(c, user['id'])


class RequestDeletion(BaseModel):
    preview_sha256: str = Field(min_length=64, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    confirmation: str


@router.post('', status_code=202)
async def begin(body: RequestDeletion, user=Depends(current_user)):
    if body.confirmation != 'DELETE MY ACCOUNT':
        raise HTTPException(400, 'Type DELETE MY ACCOUNT to confirm')
    async with db() as c:
        async with c.transaction():
            row = await c.fetchrow('SELECT * FROM users WHERE id=$1 FOR UPDATE', user['id'])
            if not row or row['account_state'] != 'active':
                raise HTTPException(409, 'Deletion already started or account unavailable')
            if not row['password_hash'] or not await asyncio.to_thread(check_password, body.password, row['password_hash']):
                raise HTTPException(403, 'Password confirmation failed')
            value = await preview_data(c, user['id'])
            if value['owned_organizations']:
                raise HTTPException(409, 'Transfer organization ownership before deleting this account')
            if value['preview_sha256'] != body.preview_sha256:
                raise HTTPException(409, 'Account inventory changed. Review a fresh deletion preview')
            jid, receipt = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
            await c.execute('INSERT INTO account_deletions(id,account_key,user_id,receipt_hash,counts) VALUES($1,$2,$3,$4,$5::jsonb)', jid, row['account_key'], user['id'], token_hash(receipt), json.dumps(value['counts']))
            await disable(c, user['id'])
            # Write-ahead consent tombstone precedes destructive work. Preserve it outside restored backups.
            await asyncio.to_thread(write_tombstone, row['account_key'])
    return {'id': jid, 'receipt': receipt, 'status': 'queued', 'notice': 'Access revoked. Keep this private receipt to check deletion progress. Backup erasure follows the operator retention policy.'}


class Receipt(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    receipt: str = Field(min_length=20, max_length=200)


@router.post('/status')
async def status(body: Receipt):
    async with db() as c:
        row = await c.fetchrow('SELECT id,status,attempts,error,counts,created_at,updated_at FROM account_deletions WHERE id=$1 AND receipt_hash=$2', body.id, token_hash(body.receipt))
    if not row:
        raise HTTPException(404, 'Deletion receipt unavailable')
    return dict(row, counts=decode(row['counts']))


@router.post('/retry')
async def retry(body: Receipt):
    async with db() as c:
        result = await c.execute("UPDATE account_deletions SET status='queued',error=NULL WHERE id=$1 AND receipt_hash=$2 AND status='failed'", body.id, token_hash(body.receipt))
    if result == 'UPDATE 0':
        raise HTTPException(404, 'Retry unavailable')
    return {'status': 'queued'}


async def apply_tombstones():
    root = ledger_root()
    if not root.exists():
        return
    for path in root.glob('*.json'):
        item = json.loads(path.read_text(encoding='utf-8'))
        key = item['account_key']
        if not re.fullmatch('[a-f0-9]{32}', key) or path.stem != key:
            raise RuntimeError('Invalid deletion ledger; restore remains blocked')
        async with db() as c:
            async with c.transaction():
                user = await c.fetchrow('SELECT id FROM users WHERE account_key=$1 FOR UPDATE', key)
                if not user:
                    continue
                await disable(c, user['id'])
                await c.execute("INSERT INTO account_deletions(id,account_key,user_id,receipt_hash) VALUES($1,$2,$3,$4) ON CONFLICT(account_key) DO UPDATE SET user_id=EXCLUDED.user_id,status='queued',error=NULL", secrets.token_urlsafe(18), key, user['id'], token_hash(secrets.token_urlsafe(32)))


def remove_owned_tree(key):
    if not re.fullmatch('[a-f0-9]{32}', key):
        raise ValueError('Invalid account key')
    path = safe_path('accounts/' + key)
    if path.exists():
        shutil.rmtree(path)


async def deletion_once():
    async with db() as c:
        # Shares the archive worker lock to prevent deleting files while an export is writing.
        if not await c.fetchval('SELECT pg_try_advisory_lock(73722903)'):
            return
        try:
            await c.execute("UPDATE account_deletions SET status='queued' WHERE status='running'")
            job = await c.fetchrow("UPDATE account_deletions SET status='running',attempts=attempts+1,updated_at=NOW() WHERE id=(SELECT id FROM account_deletions WHERE status='queued' ORDER BY created_at LIMIT 1) RETURNING *")
            if not job:
                return
            try:
                await asyncio.to_thread(write_tombstone, job['account_key'])
                async with c.transaction():
                    await c.execute("SELECT set_config('app.privacy_purge','on',true)")
                    user = await c.fetchrow('SELECT * FROM users WHERE account_key=$1 FOR UPDATE', job['account_key'])
                    removed_files = 0
                    if user:
                        if await c.fetchval("SELECT 1 FROM organization_members WHERE user_id=$1 AND role='owner'", user['id']):
                            raise ValueError('Organization ownership blocks purge')
                        for asset in await c.fetch('SELECT storage_key FROM owned_assets WHERE user_id=$1', user['id']):
                            if not await c.fetchval('SELECT 1 FROM owned_assets WHERE storage_key=$1 AND user_id<>$2', asset['storage_key'], user['id']):
                                safe_path(asset['storage_key']).unlink(missing_ok=True)
                                removed_files += 1
                        for export in await c.fetch('SELECT id FROM portability_jobs WHERE user_id=$1', user['id']):
                            for suffix in ('.zip', '.partial'):
                                safe_path('exports/' + export['id'] + suffix).unlink(missing_ok=True)
                        remove_owned_tree(job['account_key'])
                        await c.execute('DELETE FROM users WHERE id=$1', user['id'])
                    await c.execute("UPDATE account_deletions SET status='completed',error=NULL,counts=counts || jsonb_build_object('attributable_files_removed',$2::int),updated_at=NOW() WHERE id=$1", job['id'], removed_files)
            except Exception:
                await c.execute("UPDATE account_deletions SET status='failed',error='Cleanup could not finish. Access remains revoked. Retry after resolving file access or organization ownership; no deleted content is recorded in this receipt.',updated_at=NOW() WHERE id=$1", job['id'])
        finally:
            await c.execute('SELECT pg_advisory_unlock(73722903)')


async def worker():
    while True:
        try:
            await deletion_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(1)
