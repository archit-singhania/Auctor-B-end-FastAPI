"""
app/routers/score.py

GET /api/score?user_id={uid}
─────────────────────────────
Reads the latest computed score from the scores table.
Score is recalculated automatically whenever verifications or badges change.
"""

from fastapi import APIRouter, HTTPException, Query

from app.db import get_conn, release_conn
from app.models.score import AuctorScoreResponse
from app.services.score_service import ScoreService

router = APIRouter()


@router.get("/score", response_model=AuctorScoreResponse)
async def get_score(
    user_id: int = Query(1, description="User ID"),
) -> AuctorScoreResponse:
    """
    Return the latest Auctor score for a user from the DB.
    If no score row exists yet, calculate and return a zero score.
    """
    conn = await get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT total, github, leetcode, badges, projects, experience FROM scores WHERE user_id=$1",
            user_id,
        )
    finally:
        await release_conn(conn)

    if row is None:
        return AuctorScoreResponse(
            total=0.0, github=0.0, leetcode=0.0,
            badges=0.0, projects=0.0, experience=0.0,
        )

    return AuctorScoreResponse(
        total=float(row["total"]),
        github=float(row["github"]),
        leetcode=float(row["leetcode"]),
        badges=float(row["badges"]),
        projects=float(row["projects"]),
        experience=float(row["experience"]),
    )
