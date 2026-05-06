"""
app/routers/verify.py

GET /api/verify/github?username={username}&user_id={uid}
─────────────────────────────────────────────────────────
1. Call GitHub API to get user profile + repos
2. Cross-match repos against cv_data.projects from DB
3. UPSERT verifications table
4. Recalculate and update scores table
5. Return GitHubVerifyResponse
"""

import json
import httpx
from fastapi import APIRouter, HTTPException, Query

from app.db import get_conn, release_conn
from app.models.github import GitHubVerifyResponse
from app.services.github_service import GitHubService
from app.services.score_service import ScoreService

router = APIRouter()
_github = GitHubService()
_scorer = ScoreService()


@router.get("/github", response_model=GitHubVerifyResponse)
async def verify_github(
    username: str = Query(..., description="GitHub username"),
    user_id: int = Query(1, description="User ID"),
) -> GitHubVerifyResponse:
    """
    Verify a GitHub username, cross-check repos with CV projects, save result.
    """
    if not username or len(username) > 39:
        raise HTTPException(400, "Invalid GitHub username")

    # ── Load CV projects from DB to cross-match ────────────────────────────
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT projects FROM cv_data WHERE user_id = $1 ORDER BY parsed_at DESC LIMIT 1",
            user_id,
        )
        cv_projects: list[str] = []
        if row:
            projects_raw = row["projects"]
            # asyncpg returns jsonb as a string
            projects = json.loads(projects_raw) if isinstance(projects_raw, str) else projects_raw
            cv_projects = [p.get("name", "") for p in projects if p.get("name")]
    finally:
        await release_conn(conn)

    # ── Call GitHub API ────────────────────────────────────────────────────
    try:
        result = await _github.verify(username, cv_projects)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        # 401/403 from GitHub — token missing or invalid
        raise HTTPException(401, str(exc)) from exc
    except httpx.ConnectError as exc:
        raise HTTPException(503, "Cannot reach GitHub API — check Railway's network access.") from exc
    except Exception as exc:
        raise HTTPException(500, f"GitHub verification failed: {exc}") from exc

    # ── Persist verification result ────────────────────────────────────────
    conn = await get_conn()
    try:
        detail = json.dumps({
            "repos": result.repos,
            "stars": result.stars,
            "matched_projects": result.matched_projects,
            "profile_url": result.profile_url,
        })
        await conn.execute(
            """
            INSERT INTO verifications (user_id, platform, status, detail, verified_at)
            VALUES ($1, 'github', $2, $3::jsonb, NOW())
            ON CONFLICT (user_id, platform)
            DO UPDATE SET
                status      = EXCLUDED.status,
                detail      = EXCLUDED.detail,
                verified_at = EXCLUDED.verified_at
            """,
            user_id,
            "verified" if result.verified else "failed",
            detail,
        )

        # ── Recalculate score ──────────────────────────────────────────────
        await _update_score(conn, user_id)
    finally:
        await release_conn(conn)

    return result


async def _update_score(conn, user_id: int) -> None:
    """Recalculate and persist the Auctor score for a user."""
    # Load all signals from DB
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
    projects_verified = 0  # set later when project verification is added

    scorer = ScoreService()
    score = scorer.calculate(
        github_verified=github_verified,
        badges_earned=badges_earned,
        projects_verified=projects_verified,
        total_projects=total_projects,
        experience_verified=experience_verified,
    )

    await conn.execute(
        """
        INSERT INTO scores (user_id, total, github, leetcode, badges, projects, experience, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (user_id)
        DO UPDATE SET
            total       = EXCLUDED.total,
            github      = EXCLUDED.github,
            leetcode    = EXCLUDED.leetcode,
            badges      = EXCLUDED.badges,
            projects    = EXCLUDED.projects,
            experience  = EXCLUDED.experience,
            updated_at  = EXCLUDED.updated_at
        """,
        user_id,
        score.total, score.github, score.leetcode,
        score.badges, score.projects, score.experience,
    )
