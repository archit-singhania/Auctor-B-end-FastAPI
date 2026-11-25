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

    def pg(tool, database, *args):
        exe = Path(tool_dir) / (tool + ('.exe' if os.name == 'nt' else ''))
        environment = {**os.environ, 'PGHOST': parsed.hostname,
                       'PGPORT': str(parsed.port or 5432),
                       'PGUSER': unquote(parsed.username or ''),
                       'PGPASSWORD': unquote(parsed.password or '')}
        subprocess.run([str(exe), '--no-password', '--dbname', database, *map(str, args)],
                       env=environment, check=True, timeout=60)

    def register(client, handle):
        credentials = {'email': handle + '@example.test', 'password': 'recovery-password-123'}
        response = client.post('/api/auth/register', json={
            **credentials, 'handle': handle, 'display_name': handle,
        })
        assert response.status_code == 201, response.text
        return {'Authorization': 'Bearer ' + response.json()['token']}, credentials

    ledger = tmp_path / 'protected-ledger'
    live_assets = tmp_path / 'live-assets'
    restored_assets = tmp_path / 'restored-assets'
    archive = tmp_path / 'database.dump'
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
        with ZipFile(next(backups.glob('auctor-backup-*.zip'))) as bundle:
            manifest = json.loads(bundle.read('manifest.json'))
            assert manifest['format_version'] == 1
            assert set(bundle.namelist()) == {*manifest['entries'], 'manifest.json'}
            for name, info in manifest['entries'].items():
                content = bundle.read(name)
                assert info == {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
                destination = archive if name == 'database.dump' else restored_assets / name.removeprefix('assets/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        with TestClient(app) as client:
            preview = client.get('/api/account-deletion/preview', headers=deleted).json()
            started = client.post('/api/account-deletion', headers=deleted, json={
                'preview_sha256': preview['preview_sha256'], 'password': credentials['password'],
                'confirmation': 'DELETE MY ACCOUNT',
            })
            assert started.status_code == 202, started.text
        pg('pg_restore', target, '--exit-on-error', '--no-owner', archive)
        assert list(restored_assets.rglob('*.pdf'))
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
            while list(restored_assets.rglob('*.pdf')) and time.monotonic() < deadline:
                time.sleep(.05)
            assert not list(restored_assets.rglob('*.pdf'))

            async def remaining_account():
                from app.platform import db
                async with db() as connection:
                    return await connection.fetchval('SELECT id FROM users WHERE id=$1', deleted_id)

            assert recovered.portal.call(remaining_account) is None
    finally:
        for name in reversed(databases):
            asyncio.run(database_command(f'DROP DATABASE "{name}" WITH (FORCE)'))
