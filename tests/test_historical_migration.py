"""Exercise application startup against a pre-deletion historical schema."""

import asyncio
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.mark.parametrize('revision', [
    '2b8fb9fcd22b1a0a220e5bd37336e269a269fa67',
    '27f46b798d22b83ef5444234d23182f5ffdea985',
], ids=['initial-schema', 'pre-deletion-schema'])
def test_historical_schema_preserves_user_and_cv_across_startups(tmp_path, monkeypatch, revision):
    if os.getenv('AUCTOR_TEST_LOCAL') != '1':
        pytest.skip('Set AUCTOR_TEST_LOCAL=1 for disposable local migration coverage')
    dsn = os.getenv('AUCTOR_TEST_DSN', settings.db_dsn)
    if urlsplit(dsn).hostname not in {'localhost', '127.0.0.1', '::1'}:
        pytest.fail('Migration test requires local PostgreSQL')
    schema = 'auctor_migration_' + secrets.token_hex(8)
    has_review_history = revision.startswith('27f46b7')
    old_sql = subprocess.check_output(
        ['git', 'show', f'{revision}:schema.sql'],
        cwd=Path(__file__).resolve().parents[1], encoding='utf-8', timeout=10,
    )
    old_sql = (old_sql.replace('CREATE SCHEMA IF NOT EXISTS auctor', f'CREATE SCHEMA IF NOT EXISTS {schema}')
               .replace('SET search_path = auctor', f'SET search_path = {schema}')
               .replace('auctor.', f'{schema}.'))
    monkeypatch.setattr(settings, 'database_url', dsn)
    monkeypatch.setattr(settings, 'db_schema', schema)
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'storage_path', str(tmp_path / 'assets'))
    monkeypatch.setattr(settings, 'deletion_ledger_path', str(tmp_path / 'ledger'))

    async def execute(operation):
        connection = await asyncpg.connect(dsn, server_settings={'search_path': schema})
        try:
            return await operation(connection)
        finally:
            await connection.close()

    async def seed(connection):
        await connection.execute(old_sql)
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='users' AND column_name='account_state')", schema,
        )
        user_id = await connection.fetchval(
            "INSERT INTO users(handle,display_name) VALUES('historical-fixture','Historical Fixture') RETURNING id"
        )
        await connection.execute('INSERT INTO cv_data(user_id,skills) VALUES($1,$2::jsonb)', user_id, '["Python"]')
        await connection.execute(
            "INSERT INTO verifications(user_id,platform,status,detail) VALUES($1,'github','pending',$2::jsonb)",
            user_id, '{"legacy": true}',
        )
        if has_review_history:
            reviewer_id = await connection.fetchval(
                "INSERT INTO users(handle,display_name) VALUES('historical-reviewer','Historical Reviewer') RETURNING id"
            )
            await connection.execute(
                """INSERT INTO evidence(id,user_id,kind,title,status,reviewer_id,review_note)
                   VALUES('legacy-proof',$1,'certificate','Legacy proof','verified',$2,'Inspected original')""",
                user_id, reviewer_id,
            )
            await connection.execute(
                """INSERT INTO review_decisions(evidence_id,user_id,reviewer_id,title,kind,status,note,source)
                   VALUES('legacy-proof',$1,$2,'Legacy proof','certificate','verified','Inspected original',
                          '{"document_id":"original-source"}'::jsonb)""",
                user_id, reviewer_id,
            )
            await connection.execute(
                "INSERT INTO shares(id,user_id,revoked) VALUES('revoked-legacy-walkthrough',$1,TRUE)", user_id,
            )
            await connection.execute(
                """INSERT INTO recruiter_presentation_shares(share_id,data)
                   VALUES('revoked-legacy-walkthrough','{"title":"Frozen legacy walkthrough"}'::jsonb)"""
            )
        return user_id

    async def readback(connection):
        return await connection.fetch(
            "SELECT u.id,u.account_state,u.display_name,c.skills::text,c.profiles::text,"
            "v.status,v.detail->>'legacy' FROM users u JOIN cv_data c ON u.id=c.user_id "
            "JOIN verifications v ON u.id=v.user_id WHERE u.handle='historical-fixture'"
        )

    async def review_readback(connection):
        decisions = await connection.fetch(
            """SELECT e.status,r.note,r.source->>'document_id',u.handle
               FROM evidence e JOIN review_decisions r ON e.id=r.evidence_id
               JOIN users u ON u.id=r.reviewer_id WHERE e.id='legacy-proof'"""
        )
        publication = await connection.fetch(
            """SELECT s.revoked,p.data->>'title' FROM shares s
               JOIN recruiter_presentation_shares p ON s.id=p.share_id
               WHERE s.id='revoked-legacy-walkthrough'"""
        )
        return [tuple(row) for row in decisions], [tuple(row) for row in publication]

    try:
        user_id = asyncio.run(execute(seed))
        for _ in range(2):
            with TestClient(app) as client:
                assert client.get('/openapi.json').status_code == 200
                if has_review_history:
                    assert client.get('/api/share/revoked-legacy-walkthrough').status_code == 404
            assert [tuple(row) for row in asyncio.run(execute(readback))] == [
                (user_id, 'active', 'Historical Fixture', '["Python"]', '{}', 'pending', 'true'),
            ]
            if has_review_history:
                assert asyncio.run(execute(review_readback)) == (
                    [('verified', 'Inspected original', 'original-source', 'historical-reviewer')],
                    [(True, 'Frozen legacy walkthrough')],
                )
    finally:
        asyncio.run(execute(lambda connection: connection.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')))
