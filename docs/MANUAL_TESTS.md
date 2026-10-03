# Auctor API: local manual test and release guide

This guide uses disposable local accounts and labelled synthetic documents. It preserves existing data. The UI guide in the companion Flutter repository covers all 20 connected capabilities; this guide adds backend checks and setup. Do not run fixture registration or integration tests against a hosted production instance.

## Current local release

The API preview is `http://localhost:8011`; the Flutter preview is `http://localhost:8041`. `/health` must return HTTP 200 with `status: ready`, `database: connected`, `schema_version: 2` and `service: auctor-api`. Development also reports `environment: development`, `storage: local-private`. Preview processes only run while this computer keeps them open. No hosting or provider rotation was performed.

Verified: **18 backend tests**, including real PostgreSQL isolation/text-PDF jobs, private-file access, ownership, versions/grading/replay, discovery/exports, provider-boundary OAuth, graph/roadmap/badge details, issuer/audit/history comparisons, bounded unverified profile imports and production guards. Remote CI/live consent remain separate gates.

## Start the API

From PowerShell:

```powershell
Set-Location 'D:\remaining-4-git-projs\auctor\Auctor-B-end-FastAPI'
# This workspace already has a private .env.local and .venv. Preserve them.
# On a fresh clone instead: py -3 -m venv .venv
# .\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest
# Copy .env.example to .env.local only when no private configuration exists;
# edit it locally with your PostgreSQL DSN. Never print or commit it.
$env:APP_ENV='development'
$env:ALLOWED_ORIGINS='http://localhost:8041'
$env:WEB_URL='http://localhost:8041'
$env:OPENAI_API_KEY=''   # deterministic local heuristic extraction; no paid calls
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

Use an available port if 8011 is occupied, and rebuild the Flutter client with the same port. `DATABASE_URL` takes priority over individual `DB_*` values; `DB_SCHEMA` defaults to `auctor`. PostgreSQL must already run. API startup applies additive schema migrations; a database/migration failure stops startup. It does not create or delete a PostgreSQL database.

In another PowerShell window:

```powershell
Invoke-RestMethod 'http://localhost:8011/health'
Start-Process 'http://localhost:8011/docs'
```

Expected: the health fields above; OpenAPI shows `/api/auth`, `/api/cv`, `/api/attempts`, `/api/evidence`, `/api/reviews`, `/api/github`, `/api/candidates`, `/api/shares` and exports. Old `/api/score?user_id=1` returns 404. `/api/me` without a bearer token returns 401.

## Create synthetic documents

```powershell
Set-Location 'D:\remaining-4-git-projs\auctor\Auctor-B-end-FastAPI'
.\.venv\Scripts\python.exe scripts/create_manual_fixtures.py
```

The ignored `_data/manual-fixtures` directory contains a labelled text CV/proof, non-PDF, damaged/blank PDF and coding-profile JSON. Nothing is uploaded or inserted automatically. Upload `synthetic-cv.pdf` via **Evidence → Upload CV**: queued/running/succeeded, skills and a revision; all claims remain unverified. Exact heuristic segmentation may need **Review & edit**. Invalid PDF returns 400; blank/damaged input may queue then fail safely. Retry can fail again, never fabricate data. Import `coding-profile.json` through the connected JSON picker: 150 claimed problems, pending/unverified, source/digest metadata, zero points until independent review; no provider call is made.

## API ownership checks without exposing tokens

Use different `example.test` addresses/handles per run. The password below is deliberately a local test value. Keep returned session tokens in variables; do not paste them into documentation or logs.

```powershell
$auctorApi='http://localhost:8011'
$auctorRun=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$auctorFirst=Invoke-RestMethod "$auctorApi/api/auth/register" -Method Post -ContentType 'application/json' -Body (@{
  email="manual-a-$auctorRun@example.test"; password='manual-fixture-pass-123'; handle="manual-a-$auctorRun"; display_name='Manual QA A (synthetic)'
} | ConvertTo-Json)
$auctorSecond=Invoke-RestMethod "$auctorApi/api/auth/register" -Method Post -ContentType 'application/json' -Body (@{
  email="manual-b-$auctorRun@example.test"; password='manual-fixture-pass-123'; handle="manual-b-$auctorRun"; display_name='Manual QA B (synthetic)'
} | ConvertTo-Json)
$auctorHeadersA=@{Authorization="Bearer $($auctorFirst.token)"}
$auctorHeadersB=@{Authorization="Bearer $($auctorSecond.token)"}
$auctorCv=@{data=@{skills=@('Docker','PostgreSQL'); projects=@(@{name='Manual QA API';description='Synthetic fixture';tech_stack=@('Docker')}); experience=@();profiles=@{email='private-fixture@example.test'}};note='Manual synthetic CV'}
$null=Invoke-RestMethod "$auctorApi/api/cv" -Method Put -Headers $auctorHeadersA -ContentType 'application/json' -Body ($auctorCv | ConvertTo-Json -Depth 8)
$auctorReadA=Invoke-RestMethod "$auctorApi/api/me" -Headers $auctorHeadersA
$auctorReadB=Invoke-RestMethod "$auctorApi/api/me?user_id=$($auctorReadA.profile.id)" -Headers $auctorHeadersB
$auctorReadA.cv.skills
$auctorReadB.cv.skills
```

Expected: A has Docker/PostgreSQL and a version, B remains empty even when an arbitrary A user ID appears in the query. B cannot access A's job/source, revision, attempt, evidence proof or revoke A's share: return 404, or 403 for a role-protected review endpoint. Registration never grants reviewer privilege from an email address. Wrong password returns 401 with a generic message; duplicate email/handle returns 409. Missing/invalid fields return 422. Repeated abuse can hit 429; wait for the limit window rather than disabling controls.

In OpenAPI, use **Authorize** with the local token privately to inspect requests. A request with `is_verified: true` in an edited CV must read back false for claims; only ownership/reviewer operations contribute evidence points.

## Grading and replay check

Use the UI to answer **Docker** with the supplied local practice key in the Flutter guide. Fresh account expected result: 5/5, passed, actual delta **+0.6** and total **0.6/10**. In OpenAPI inspect the attempt creation response: five prompts/options, owned attempt ID and server expiry; no correct-answer field. Preserve the submitted attempt ID/body from browser Network in the test session, and replay that exact POST once. Expected: `replayed: true`, original result, and total remains 0.6. Submitting with B's token returns 404. An incomplete/invalid answer map returns 422. After five minutes an unsubmitted attempt returns 409; the UI disables submission at expiry. Re-passing the same track adds zero points; failing a later attempt preserves the earlier badge. Ten attempts within an hour can return 429.

## Reviewer and private proof checks

1. Register a separate local reviewer through the UI. A regular account has no Reviews navigation and `/api/reviews` returns 403.
2. Grant only this deliberate local test reviewer, from the API repository with private database access:

   ```powershell
   .\.venv\Scripts\python.exe -m app.manage grant-reviewer manual-reviewer@example.test
   ```

3. Sign out/in or refresh the reviewer session. **Reviews** appears. Developer A creates experience/certificate/coding evidence, then **Attach PDF proof** with `synthetic-proof.pdf`. The record stays pending and has no points until a review.
4. Reviewer downloads the source, writes a rationale of at least 10 characters explicitly saying it is a synthetic software-test fixture, and records approval/rejection. Owner sees the decision and activity after refresh. Reviewed experience contributes at most 1.5 points; reviewed 150-problem coding claim contributes 0.75. Certificates display reviewed evidence without v1 score points. A rejected submission contributes none.
5. Reviewer cannot review their own evidence (403). Another developer cannot download A's proof (404). Approved proof cannot be overwritten; a new submission is required (409). These are software workflow checks, never a claim that synthetic employment is real.

## Sharing and persistence checks

- A creates a private link while discovery is off. Anonymous `/api/share/<id>` shows the redacted profile, no private contact email/phone or source/proof bytes. Anonymous `/api/public/<handle>` remains 404 until opt-in.
- A revokes the link; anonymous access becomes 404. B's revoke request cannot revoke it. Revoke survives reload and API restart.
- Public SVG badge returns 404 while discovery is off. Enabling discovery makes the profile, candidate search and SVG available. Turning it off removes public profile/search/embed; separately created private links remain controlled by their own revoke setting.
- Restart only your own API process and reload the UI. CV/revisions/jobs/scores/preferences/decisions/shares/candidate saves remain in PostgreSQL. Private source files remain at the same `STORAGE_PATH`; changing/deleting it loses source access. Sessions expire after 30 days and logout invalidates the current bearer token.
- JSON export must parse and PDF export must open; both use the authenticated owner's record. Other users cannot choose an arbitrary owner by query parameter.

## Automated checks and isolation

```powershell
Set-Location 'D:\remaining-4-git-projs\auctor\Auctor-B-end-FastAPI'
$env:AUCTOR_TEST_LOCAL='1'
New-Item -ItemType Directory -Force -Path '_data/qa-temp' | Out-Null
$env:TEMP='D:\remaining-4-git-projs\auctor\Auctor-B-end-FastAPI\_data\qa-temp'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
# Optional AUCTOR_TEST_DSN: a private DSN to a LOCAL disposable PostgreSQL database.
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 18 passed with local PostgreSQL available. Integration tests create a random `auctor_test_*` schema and temporary private-file directory; teardown removes only that generated schema. They refuse non-local PostgreSQL hosts. Without `AUCTOR_TEST_LOCAL=1`, integration checks are skipped and a green result is not full integration verification. OAuth tests use explicit provider fixtures; no live GitHub/OpenAI calls are claimed.

## Optional gates

GitHub ownership requires your own OAuth app ID/secret and exact `GITHUB_REDIRECT_URI=http://localhost:8011/api/github/callback`, then interactive personal consent. Set `WEB_URL`/CORS to the running frontend origin. Without both OAuth settings, **Connect GitHub** must report unconfigured rather than claim verification. OpenAI is optional; leaving it empty exercises the heuristic parser. Live provider results/costs and native devices require separate validation.

Production is deferred. Read the companion Flutter `docs/DEPLOYMENT.md` before later publishing: rotate formerly tracked credentials, configure HTTPS, a private persistent volume and backups, set `APP_ENV=production`, and validate `/health`. No original records, secrets, Git history or hosted resources were deleted by this release.
