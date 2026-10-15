-- ============================================================
-- Auctor Schema
-- ============================================================

CREATE SCHEMA IF NOT EXISTS auctor;

SET search_path = auctor;

-- ── users ─────────────────────────────────────────────────────────────────────
-- One row per registered developer.
-- handle = short username (e.g. 'john_doe'), used for shareable profile URLs.
CREATE TABLE IF NOT EXISTS auctor.users (
    id           SERIAL PRIMARY KEY,
    handle       TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email        TEXT UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── cv_data ───────────────────────────────────────────────────────────────────
-- Stores the structured output of the most-recent CV parse for each user.
-- skills / projects / experience / profiles are JSONB so the schema stays
-- flexible as the CV parser evolves without needing migrations.
-- A user can re-upload; the router deletes the old row and inserts fresh.
CREATE TABLE IF NOT EXISTS auctor.cv_data (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    skills      JSONB NOT NULL DEFAULT '[]',
    projects    JSONB NOT NULL DEFAULT '[]',
    experience  JSONB NOT NULL DEFAULT '[]',
    profiles    JSONB NOT NULL DEFAULT '{}',
    parsed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS cv_data_user_idx ON auctor.cv_data(user_id);

-- Migration: add profiles column if upgrading from an older schema
ALTER TABLE auctor.cv_data ADD COLUMN IF NOT EXISTS profiles JSONB NOT NULL DEFAULT '{}';

-- ── verifications ─────────────────────────────────────────────────────────────
-- One row per (user × platform) verification.
-- platform values: 'github' | 'leetcode' | 'certificate' | 'experience'
-- status  values: 'pending' | 'verified' | 'failed'
-- detail: free-form JSON blob — for GitHub: {repos, stars, matched_projects}
CREATE TABLE IF NOT EXISTS auctor.verifications (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    detail      JSONB NOT NULL DEFAULT '{}',
    verified_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, platform)
);
CREATE INDEX IF NOT EXISTS verifications_user_idx ON auctor.verifications(user_id);

-- ── badges ────────────────────────────────────────────────────────────────────
-- One row per (user × badge_id).  ON CONFLICT updates on re-attempt.
-- badge_id examples: 'jwt-auth', 'docker', 'rest-api', 'postgres', 'redis'
-- score_gained: the fractional score contribution (0.0–1.0), e.g. 0.8 on pass
CREATE TABLE IF NOT EXISTS auctor.badges (
    id              SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    badge_id        TEXT NOT NULL,
    badge_name      TEXT NOT NULL,
    skill           TEXT NOT NULL,
    passed          BOOLEAN NOT NULL DEFAULT FALSE,
    correct_answers INT NOT NULL DEFAULT 0,
    total_questions INT NOT NULL DEFAULT 0,
    score_gained    NUMERIC(5, 2) NOT NULL DEFAULT 0,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, badge_id)
);
CREATE INDEX IF NOT EXISTS badges_user_idx ON auctor.badges(user_id);

-- ── scores ────────────────────────────────────────────────────────────────────
-- One row per user — the LATEST computed Auctor score.
-- Recalculated (UPSERT) every time a verification or badge event fires.
-- total   : 0.0 – 10.0  (weighted sum of components × 10)
-- github  : 0.0 – 1.0   (fraction of GitHub weight earned)
-- leetcode: 0.0 – 1.0
-- badges  : 0.0 – 1.0
-- projects: 0.0 – 1.0
-- experience: 0.0 – 1.0
CREATE TABLE IF NOT EXISTS auctor.scores (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    total       NUMERIC(5, 2) NOT NULL DEFAULT 0,
    github      NUMERIC(6, 4) NOT NULL DEFAULT 0,
    leetcode    NUMERIC(6, 4) NOT NULL DEFAULT 0,
    badges      NUMERIC(6, 4) NOT NULL DEFAULT 0,
    projects    NUMERIC(6, 4) NOT NULL DEFAULT 0,
    experience  NUMERIC(6, 4) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── seed: demo user ───────────────────────────────────────────────────────────
-- This ensures user_id=1 exists so the app works immediately on first run
-- without any registration flow (MVP shortcut).
INSERT INTO auctor.users (handle, display_name)
VALUES ('demo', 'Developer')
ON CONFLICT (handle) DO NOTHING;

-- Give the demo user a zero-score row so GET /api/score returns 0 instead of null.
INSERT INTO auctor.scores (user_id)
SELECT id FROM users WHERE handle = 'demo'
ON CONFLICT (user_id) DO NOTHING;

-- Premium platform migration v2. Existing MVP data is retained for an explicit migration.
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'developer';
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT '';
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS discoverable BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}';
ALTER TABLE auctor.users ADD COLUMN IF NOT EXISTS github_identity JSONB NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS auctor.sessions (token_hash TEXT PRIMARY KEY, user_id INT REFERENCES auctor.users(id) ON DELETE CASCADE, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS auctor.documents (id TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, filename TEXT NOT NULL, storage_key TEXT, status TEXT NOT NULL DEFAULT 'queued', error TEXT, data JSONB NOT NULL DEFAULT '{}', source JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS documents_owner_idx ON auctor.documents(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS auctor.cv_versions (id SERIAL PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, document_id TEXT REFERENCES auctor.documents(id), data JSONB NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.evidence (id TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', detail JSONB NOT NULL DEFAULT '{}', storage_key TEXT, status TEXT NOT NULL DEFAULT 'declared', reviewer_id INT REFERENCES auctor.users(id), review_note TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS evidence_owner_idx ON auctor.evidence(user_id,kind);
CREATE TABLE IF NOT EXISTS auctor.attempts (id TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, badge_id TEXT NOT NULL, question_ids JSONB NOT NULL, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL, submitted_at TIMESTAMPTZ, answers JSONB, correct_count INT, passed BOOLEAN, score_delta NUMERIC(6,2));
CREATE INDEX IF NOT EXISTS attempts_owner_idx ON auctor.attempts(user_id,badge_id);
CREATE TABLE IF NOT EXISTS auctor.score_history (id SERIAL PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, formula_version TEXT NOT NULL DEFAULT 'v1', score JSONB NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
ALTER TABLE auctor.score_history ADD COLUMN IF NOT EXISTS signals JSONB NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS auctor.review_decisions (id SERIAL PRIMARY KEY, evidence_id TEXT NOT NULL, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, reviewer_id INT NOT NULL REFERENCES auctor.users(id), title TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, source JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS review_decisions_owner_idx ON auctor.review_decisions(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS auctor.activity (id SERIAL PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, kind TEXT NOT NULL, message TEXT NOT NULL, read BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.shares (id TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, revoked BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.saved_candidates (user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, candidate_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, PRIMARY KEY(user_id,candidate_id));
CREATE TABLE IF NOT EXISTS auctor.oauth_states (state_hash TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, expires_at TIMESTAMPTZ NOT NULL, completed BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS auctor.schema_versions (version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
ALTER TABLE auctor.documents ADD COLUMN IF NOT EXISTS run_token TEXT;
INSERT INTO auctor.schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS auctor.career_stories (user_id INT PRIMARY KEY REFERENCES auctor.users(id) ON DELETE CASCADE, revision INT NOT NULL DEFAULT 1, milestones JSONB NOT NULL DEFAULT '[]', updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
INSERT INTO auctor.schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS auctor.role_documents (user_id INT PRIMARY KEY REFERENCES auctor.users(id) ON DELETE CASCADE, revision INT NOT NULL, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS auctor.role_fit_runs (id TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS role_runs_owner_idx ON auctor.role_fit_runs(user_id,created_at DESC);
INSERT INTO auctor.schema_versions(version) VALUES (4) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS auctor.evidence_snapshots (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,kind TEXT NOT NULL,source_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',data JSONB NOT NULL,error TEXT,run_token TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS snapshot_owner_idx ON auctor.evidence_snapshots(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS auctor.evidence_comparisons (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,left_id TEXT NOT NULL REFERENCES auctor.evidence_snapshots(id),right_id TEXT NOT NULL REFERENCES auctor.evidence_snapshots(id),revision INT NOT NULL DEFAULT 0,data JSONB NOT NULL,annotations JSONB NOT NULL DEFAULT '[]',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.practice_sessions (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,revision INT NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',questions JSONB NOT NULL,source JSONB NOT NULL,answers JSONB NOT NULL DEFAULT '{}',review JSONB NOT NULL DEFAULT '{}',feedback JSONB NOT NULL DEFAULT '{}',started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),expires_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS practice_owner_idx ON auctor.practice_sessions(user_id,started_at DESC);
INSERT INTO auctor.schema_versions(version) VALUES (5) ON CONFLICT DO NOTHING;

ALTER TABLE auctor.shares ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'profile';
ALTER TABLE auctor.shares ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS auctor.recruiter_presentations (user_id INT PRIMARY KEY REFERENCES auctor.users(id) ON DELETE CASCADE,revision INT NOT NULL,data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS auctor.recruiter_presentation_shares (share_id TEXT PRIMARY KEY REFERENCES auctor.shares(id) ON DELETE CASCADE,data JSONB NOT NULL);
INSERT INTO auctor.schema_versions(version) VALUES (6) ON CONFLICT DO NOTHING;

ALTER TABLE auctor.evidence ADD COLUMN IF NOT EXISTS review_due_at TIMESTAMPTZ;
ALTER TABLE auctor.evidence ADD COLUMN IF NOT EXISTS eligibility_state TEXT NOT NULL DEFAULT 'current';
CREATE TABLE IF NOT EXISTS auctor.verification_receipts (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,evidence_id TEXT NOT NULL,payload JSONB NOT NULL,signature TEXT NOT NULL,revoked BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.evidence_lifecycle_audit (id SERIAL PRIMARY KEY,evidence_id TEXT NOT NULL,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,reviewer_id INT REFERENCES auctor.users(id),state TEXT NOT NULL,note TEXT NOT NULL,review_due_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.evidence_rechecks (id TEXT PRIMARY KEY,evidence_id TEXT NOT NULL REFERENCES auctor.evidence(id) ON DELETE CASCADE,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'pending',reason TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',reviewer_id INT REFERENCES auctor.users(id),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),decided_at TIMESTAMPTZ);
CREATE UNIQUE INDEX IF NOT EXISTS pending_recheck_unique ON auctor.evidence_rechecks(evidence_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS auctor.assessment_integrity_events (id SERIAL PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,attempt_id TEXT NOT NULL,sequence INT NOT NULL,outcome TEXT NOT NULL,elapsed_seconds DOUBLE PRECISION NOT NULL,received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(attempt_id,sequence));
CREATE TABLE IF NOT EXISTS auctor.assessment_integrity_signals (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,attempt_id TEXT NOT NULL,kind TEXT NOT NULL,data JSONB NOT NULL,review_status TEXT NOT NULL DEFAULT 'pending',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(attempt_id,kind));
CREATE TABLE IF NOT EXISTS auctor.integrity_review_audit (id SERIAL PRIMARY KEY,signal_id TEXT NOT NULL REFERENCES auctor.assessment_integrity_signals(id) ON DELETE CASCADE,reviewer_id INT REFERENCES auctor.users(id),disposition TEXT NOT NULL,note TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
INSERT INTO auctor.schema_versions(version) VALUES (7) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS auctor.recruiter_integrations (id TEXT PRIMARY KEY,user_id INT NOT NULL REFERENCES auctor.users(id) ON DELETE CASCADE,name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,scopes JSONB NOT NULL,webhook_url TEXT NOT NULL DEFAULT '',secret_cipher TEXT NOT NULL,revoked BOOLEAN NOT NULL DEFAULT FALSE,expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS auctor.integration_outbox (event_id TEXT PRIMARY KEY,integration_id TEXT NOT NULL REFERENCES auctor.recruiter_integrations(id) ON DELETE CASCADE,payload JSONB NOT NULL,status TEXT NOT NULL DEFAULT 'queued',attempts INT NOT NULL DEFAULT 0,last_error TEXT,next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),delivered_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS outbox_delivery_idx ON auctor.integration_outbox(status,next_attempt_at);
INSERT INTO auctor.schema_versions(version) VALUES (8) ON CONFLICT DO NOTHING;
