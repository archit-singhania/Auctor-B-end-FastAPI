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
-- skills / projects / experience are JSONB arrays so the schema stays flexible
-- as the CV parser evolves without needing migrations.
-- A user can re-upload; the router deletes the old row and inserts fresh.
CREATE TABLE IF NOT EXISTS auctor.cv_data (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    skills      JSONB NOT NULL DEFAULT '[]',
    projects    JSONB NOT NULL DEFAULT '[]',
    experience  JSONB NOT NULL DEFAULT '[]',
    parsed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS cv_data_user_idx ON auctor.cv_data(user_id);

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
