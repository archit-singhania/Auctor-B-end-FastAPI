"""Opt-in PostgreSQL and private-file recovery rehearsal using disposable databases."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import sys
from urllib.parse import unquote, urlsplit, urlunsplit
from zipfile import ZipFile

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_restore_dump_and_replay_deletion(tmp_path, monkeypatch):
    tool_dir = os.environ.get('AUCTOR_TEST_PG_BIN')
    if os.environ.get('AUCTOR_TEST_LOCAL') != '1' or not tool_dir:
        pytest.skip('Set AUCTOR_TEST_LOCAL and AUCTOR_TEST_PG_BIN for disposable local recovery')
    admin_dsn = os.environ.get('AUCTOR_TEST_DSN', settings.db_dsn)
    parsed = urlsplit(admin_dsn)
    if parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        pytest.fail('Recovery test requires local PostgreSQL')
    source, target = ('auctor_restore_' + secrets.token_hex(8) for _ in range(2))
    databases = []

    async def database_command(command):
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(command)
        finally:
            await conn.close()

    def dsn(name):
        return urlunsplit(parsed._replace(path='/' + name))

    def register(client, handle):
        credentials = {'email': handle + '@example.test', 'password': 'recovery-password-123'}
        response = client.post('/api/auth/register', json={
            **credentials, 'handle': handle, 'display_name': handle,
        })
        assert response.status_code == 201, response.text
        return {'Authorization': 'Bearer ' + response.json()['token']}, credentials

    ledger = tmp_path / 'protected-ledger'
    live_assets = tmp_path / 'live-assets'
    recovery = tmp_path / 'recovery'
    restored_assets = recovery / 'assets'
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'db_schema', 'auctor_recovery')
    monkeypatch.setattr(settings, 'deletion_ledger_path', str(ledger))
    monkeypatch.setattr(settings, 'database_url', dsn(source))
    monkeypatch.setattr(settings, 'storage_path', str(live_assets))
    try:
        for name in (source, target):
            asyncio.run(database_command(f'CREATE DATABASE "{name}"'))
            databases.append(name)
        with TestClient(app) as client:
            deleted, credentials = register(client, 'restore-deleted')
            retained, _ = register(client, 'restore-retained')
            assert client.patch('/api/me', headers=deleted, json={
                'display_name': 'Restore deleted', 'discoverable': True,
            }).status_code == 200
            deleted_id = client.get('/api/me', headers=deleted).json()['profile']['id']
            share = client.post('/api/shares', headers=deleted).json()['id']
            evidence = client.post('/api/evidence', headers=deleted, json={
                'kind': 'certificate', 'title': 'Recovery proof', 'detail': {},
                'url': 'https://example.test/recovery',
            }).json()['id']
            uploaded = client.post(f'/api/evidence/{evidence}/file', headers=deleted,
                                   files={'file': ('proof.pdf', b'%PDF-1.4 recovery fixture', 'application/pdf')})
            assert uploaded.status_code == 200, uploaded.text
            retained_evidence = client.post('/api/evidence', headers=retained, json={
                'kind': 'certificate', 'title': 'Retained recovery proof', 'detail': {},
                'url': 'https://example.test/retained',
            })
            assert retained_evidence.status_code == 201, retained_evidence.text
            retained_file = '/api/evidence/' + retained_evidence.json()['id'] + '/file'
            retained_bytes = b'%PDF-1.4 retained recovery fixture'
            retained_upload = client.post(retained_file, headers=retained,
                                          files={'file': ('proof.pdf', retained_bytes, 'application/pdf')})
            assert retained_upload.status_code == 200, retained_upload.text
            assert client.get('/api/share/' + share).status_code == 200
        backups = tmp_path / 'backups'
        backups.mkdir()
        expired = backups / ('auctor-backup-20200101T000000Z-' + '0' * 32 + '.zip')
        expired.write_bytes(b'expired fixture')
        unrelated = backups / 'unrelated.zip'
        unrelated.write_bytes(b'keep')
        environment = {**os.environ, 'PGHOST': parsed.hostname,
                       'PGPORT': str(parsed.port or 5432), 'PGDATABASE': source,
                       'PGUSER': unquote(parsed.username or ''),
                       'PGPASSWORD': unquote(parsed.password or '')}
        backup_script = Path(__file__).resolve().parents[1] / 'scripts' / 'backup.py'
        subprocess.run([
            sys.executable, str(backup_script), '--writes-paused', '--output', str(backups),
            '--storage', str(live_assets), '--deletion-ledger', str(ledger),
            '--pg-dump', str(Path(tool_dir) / ('pg_dump.exe' if os.name == 'nt' else 'pg_dump')),
            '--retention-days', '30',
        ], env=environment, check=True, timeout=60)
        assert not expired.exists() and unrelated.read_bytes() == b'keep'
        verified_archive = next(backups.glob('auctor-backup-*.zip'))
        subprocess.run([
            sys.executable, str(backup_script.with_name('verify_backup.py')),
            str(backups), '--latest', '--max-age-hours', '25',
        ], check=True, timeout=60)
        corrupt = backups / ('auctor-backup-99991231T235959Z-' + '0' * 32 + '.zip')
        corrupt.write_bytes(b'incomplete fixture')
        try:
            rejected = subprocess.run([
                sys.executable, str(backup_script.with_name('verify_backup.py')),
                str(backups), '--latest', '--max-age-hours', '25',
            ], capture_output=True, text=True, timeout=60)
            assert rejected.returncode == 1
            assert 'not a zip file' in rejected.stderr.lower()
        finally:
            corrupt.unlink()
        with ZipFile(verified_archive) as bundle:
            manifest = json.loads(bundle.read('manifest.json'))
            assert manifest['format_version'] == 1
            assert set(bundle.namelist()) == {*manifest['entries'], 'manifest.json'}
            for name, info in manifest['entries'].items():
                content = bundle.read(name)
                assert info == {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
            stale_archive = tmp_path / 'stale.zip'
            with ZipFile(stale_archive, 'w') as stale:
                for name in manifest['entries']:
                    stale.writestr(name, bundle.read(name))
                stale.writestr('manifest.json', json.dumps({**manifest, 'created_at': '2020-01-01T00:00:00Z'}))
        rejected = subprocess.run([
            sys.executable, str(backup_script.with_name('verify_backup.py')),
            str(stale_archive), '--max-age-hours', '25',
        ], capture_output=True, text=True, timeout=60)
        assert rejected.returncode == 1
        assert '25-hour freshness limit' in rejected.stderr
        with TestClient(app) as client:
            preview = client.get('/api/account-deletion/preview', headers=deleted).json()
            started = client.post('/api/account-deletion', headers=deleted, json={
                'preview_sha256': preview['preview_sha256'], 'password': credentials['password'],
                'confirmation': 'DELETE MY ACCOUNT',
            })
            assert started.status_code == 202, started.text
        restore_command = [
            sys.executable, str(backup_script.with_name('restore_backup.py')), str(verified_archive),
            '--database', target,
            '--pg-restore', str(Path(tool_dir) / ('pg_restore.exe' if os.name == 'nt' else 'pg_restore')),
        ]
        subprocess.run([*restore_command, '--destination', str(recovery)],
                       env=environment, check=True, timeout=60)
        refused_destination = tmp_path / 'refused-recovery'
        refused = subprocess.run([*restore_command, '--destination', str(refused_destination)],
                                 env=environment, capture_output=True, text=True, timeout=60)
        assert refused.returncode != 0 and 'must be an empty database' in refused.stderr
        assert not refused_destination.exists()
        assert len(list(restored_assets.rglob('*.pdf'))) == 2
        settings.database_url = dsn(target)
        settings.storage_path = str(restored_assets)
        with TestClient(app) as recovered:
            assert recovered.get('/api/me', headers=deleted).status_code == 401
            assert recovered.post('/api/auth/login', json=credentials).status_code == 401
            assert recovered.get('/api/share/' + share).status_code == 404
            assert recovered.get('/api/me', headers=retained).status_code == 200
            from app.account_deletion import deletion_once
            recovered.portal.call(deletion_once)
            deadline = time.monotonic() + 5
            while len(list(restored_assets.rglob('*.pdf'))) > 1 and time.monotonic() < deadline:
                time.sleep(.05)
            assert [path.read_bytes() for path in restored_assets.rglob('*.pdf')] == [retained_bytes]
            readback = recovered.get(retained_file, headers=retained)
            assert readback.status_code == 200, readback.text
            assert readback.content == retained_bytes

            async def remaining_account():
                from app.platform import db
                async with db() as connection:
                    return await connection.fetchval('SELECT id FROM users WHERE id=$1', deleted_id)

            assert recovered.portal.call(remaining_account) is None
    finally:
        for name in reversed(databases):
            asyncio.run(database_command(f'DROP DATABASE "{name}" WITH (FORCE)'))
