"""app/models/badge.py — Badge submission request & response schemas."""

from pydantic import BaseModel, Field


class BadgeSubmitRequest(BaseModel):
    badge_id: str = Field(..., description="e.g. 'jwt-auth'")
    correct_answers: int = Field(..., ge=0)
    total_questions: int = Field(..., ge=1)


class BadgeSubmitResponse(BaseModel):
    passed: bool
    badge_id: str
    score_gained: float = Field(0.0, description="Score delta added to the user's total")
