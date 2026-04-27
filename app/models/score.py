"""app/models/score.py — AuctorScore response schema."""

from pydantic import BaseModel, Field


class AuctorScoreResponse(BaseModel):
    """
    Score breakdown returned to the Flutter app.
    Each component is a 0–1 fraction of its maximum contribution.
    'total' is the weighted sum scaled to 0–10.
    """
    total: float = Field(..., ge=0, le=10, description="Final score out of 10")
    github: float = Field(0.0, ge=0, le=1)
    leetcode: float = Field(0.0, ge=0, le=1)
    badges: float = Field(0.0, ge=0, le=1)
    projects: float = Field(0.0, ge=0, le=1)
    experience: float = Field(0.0, ge=0, le=1)
