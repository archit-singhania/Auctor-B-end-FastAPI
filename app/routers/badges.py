"""
app/routers/badges.py

POST /api/badges/submit
────────────────────────
1. Save badge attempt to DB (UPSERT — one row per user+badge)
2. Recalculate score
3. Return result
"""

import json
from fastapi import APIRouter, Query

from app.db import get_conn, release_conn
from app.models.badge import BadgeSubmitRequest, BadgeSubmitResponse
from app.services.score_service import ScoreService

router = APIRouter()

PASS_THRESHOLD = 3
SCORE_ON_PASS = 0.8

# Badge id → human name mapping
BADGE_NAMES = {
    "jwt-auth": "JWT Auth",
    "docker":   "Docker",
    "rest-api": "REST API",
    "postgres": "PostgreSQL",
    "redis":    "Redis",
}


@router.post("/submit", response_model=BadgeSubmitResponse)
async def submit_badge(
    payload: BadgeSubmitRequest,
    user_id: int = Query(1, description="User ID"),
) -> BadgeSubmitResponse:
    """
    Submit badge challenge result, persist to DB, update score.
    """
    passed = payload.correct_answers >= PASS_THRESHOLD
    score_gained = SCORE_ON_PASS if passed else 0.0
    badge_name = BADGE_NAMES.get(payload.badge_id, payload.badge_id)

    conn = await get_conn()
    try:
        await conn.execute(
            """
            INSERT INTO badges
                (user_id, badge_id, badge_name, skill, passed, correct_answers, total_questions, score_gained, attempted_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (user_id, badge_id)
            DO UPDATE SET
                passed          = EXCLUDED.passed,
                correct_answers = EXCLUDED.correct_answers,
                total_questions = EXCLUDED.total_questions,
                score_gained    = EXCLUDED.score_gained,
                attempted_at    = EXCLUDED.attempted_at
            """,
            user_id,
            payload.badge_id,
            badge_name,
            payload.badge_id,   # skill = badge_id for now
            passed,
            payload.correct_answers,
            payload.total_questions,
            score_gained,
        )

        # Recalculate and persist score
        await _update_score(conn, user_id)
    finally:
        await release_conn(conn)

    return BadgeSubmitResponse(
        passed=passed,
        badge_id=payload.badge_id,
        score_gained=score_gained,
    )


async def _update_score(conn, user_id: int) -> None:
    """Shared helper — recalculate score from all signals and persist."""
    github_row = await conn.fetchrow(
        "SELECT status FROM verifications WHERE user_id=$1 AND platform='github'", user_id
    )
    exp_row = await conn.fetchrow(
        "SELECT status FROM verifications WHERE user_id=$1 AND platform='experience'", user_id
    )
    badges_row = await conn.fetchrow(
        "SELECT COUNT(*) AS cnt FROM badges WHERE user_id=$1 AND passed=TRUE", user_id
    )
    projects_row = await conn.fetchrow(
        "SELECT projects FROM cv_data WHERE user_id=$1 ORDER BY parsed_at DESC LIMIT 1", user_id
    )

    github_verified = github_row["status"] == "verified" if github_row else False
    experience_verified = exp_row["status"] == "verified" if exp_row else False
    badges_earned = int(badges_row["cnt"]) if badges_row else 0

    total_projects = 0
    if projects_row:
        raw = projects_row["projects"]
        projs = json.loads(raw) if isinstance(raw, str) else raw
        total_projects = len(projs)

    scorer = ScoreService()
    score = scorer.calculate(
        github_verified=github_verified,
        badges_earned=badges_earned,
        projects_verified=0,
        total_projects=total_projects,
        experience_verified=experience_verified,
    )

    await conn.execute(
        """
        INSERT INTO scores (user_id, total, github, leetcode, badges, projects, experience, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (user_id)
        DO UPDATE SET
            total      = EXCLUDED.total,
            github     = EXCLUDED.github,
            leetcode   = EXCLUDED.leetcode,
            badges     = EXCLUDED.badges,
            projects   = EXCLUDED.projects,
            experience = EXCLUDED.experience,
            updated_at = EXCLUDED.updated_at
        """,
        user_id,
        score.total, score.github, score.leetcode,
        score.badges, score.projects, score.experience,
    )
