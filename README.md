# Auctor evidence API

Auctor is a developer evidence platform built with FastAPI, PostgreSQL and a Flutter client. Version 2 replaces shared-demo writes with authenticated ownership. The original MVP schema and records are preserved; legacy `/api/score?user_id=...`, `/api/verify/github` and client-reported quiz-score endpoints are intentionally unmounted.

## Run locally

Use Python 3.11+ and PostgreSQL. Create a virtual environment, install `requirements.txt`, and copy `.env.example` to ignored `.env.local`. Supply your database DSN and an exact browser origin. Run `uvicorn app.main:app --host 127.0.0.1 --port 8000`. `/health` checks PostgreSQL and migration readiness. The lifespan worker claims durable CV jobs using row locks and lease tokens; interrupted jobs become eligible after five minutes.

`schema.sql` contains additive, idempotent migrations with schema version 2. `DB_SCHEMA` supports isolated environments. Migration errors stop startup; no existing CVs or evidence are deleted. The old demo user has no authentication credentials and cannot be claimed through the new account flow.

## Capabilities and boundaries

1. Password accounts use per-password salts, PBKDF2 and expiring hashed bearer sessions. All signups are developers. An operator grants reviewer access with `python -m app.manage grant-reviewer registered@example.com`; email-based auto-promotion is forbidden.
2. GitHub OAuth confirms account ownership and imports up to 2,000 public owned repositories. Access tokens are used once and discarded; reconnect refreshes the snapshot.
3. PDF extraction is a cancellable/retriable persistent job. Limits: valid PDF magic bytes, 10 MB, 120-second parser timeout. OpenAI is optional; heuristic extraction remains available.
4. Editable CV claims, append-only versions, revision comparisons/restoration and persistent readback.
5. Evidence supports owned project binding, certificate/experience proofs and coding-profile count claims. Claims do not become verified from a URL alone.
6. Five server-managed assessment tracks with timed owned attempts, server grading and idempotent submission. Answers are not sent with question prompts. Repeated passes do not inflate score; later failed attempts preserve earlier achievements.
7. Independent reviewers inspect private proof files or sources and record a reason. Self-review is forbidden.
8. Explainable score v1 preserves weights: GitHub 25%, coding 15%, badges 30%, project evidence 15%, experience 15%. Coding uses independently reviewed counts divided by 300; certificates are visible proof but do not add v1 points.
9. Activity/read status, score history, opt-in candidate search/filter/save/compare, private revocable shares, contact-redacted public profiles, JSON/PDF reports and public SVG embed badges.

Private files live under ignored `STORAGE_PATH` and are served only to the owner or reviewer. A production instance must mount a persistent private volume, use HTTPS, restrict origins and configure backups. External object storage and malware scanning are not configured here. Create the reviewer account, verify its identity out of band, then use the operator role-assignment command; never grant general users reviewer privileges.

## API

OpenAPI `/docs` is the current contract. Core groups: `/api/auth/*`, `/api/me`, `/api/cv`, `/api/cv/jobs`, `/api/challenges`, `/api/attempts`, `/api/evidence`, `/api/reviews`, `/api/github`, `/api/candidates`, `/api/shares`, `/api/public`, `/api/share`, `/api/export`, `/api/badge`.

Every private resource derives its owner from `Authorization: Bearer ...`; arbitrary `user_id` is not an API contract. Source URLs and private proof files are never interpreted as proof of broad employment or professional competence. Auctor records assessment/evidence results, not hiring guarantees.

## Verification

`python -m pytest -q` runs pure domain tests. For integration verification set `AUCTOR_TEST_LOCAL=1`; tests create a random isolated schema on a local PostgreSQL DSN, then remove only that exact schema. `AUCTOR_TEST_DSN` can override the private local DSN. Non-local hosts are refused. CI uses a disposable PostgreSQL service.

The test journeys cover account separation, authenticated writes, server grading and replay, actual score deltas, privacy and share revocation, invalid uploads, independent reviewer decisions, discovery, PDF reports and database readiness. Live GitHub/OpenAI verification requires operator credentials and an interactive GitHub OAuth consent flow.

## Security follow-up

The formerly tracked `.env` is removed from the source change and preserved as ignored `.env.local`. This does not erase Git history or rotate credentials. Any secrets previously exposed in history must be rotated by their account owner before publishing. No values were printed and no live account credentials were changed.

