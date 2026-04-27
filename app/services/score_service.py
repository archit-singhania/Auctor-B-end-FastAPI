"""
app/services/score_service.py
──────────────────────────────
Auctor Score calculation engine.

Score formula (out of 10):
  total = (
      github_frac     * weight_github     +
      leetcode_frac   * weight_leetcode   +
      badges_frac     * weight_badges     +
      projects_frac   * weight_projects   +
      experience_frac * weight_experience
  ) * 10

Each fraction is 0–1, representing completion within that component.
Weights are loaded from Settings (configurable via .env).

This is a pure calculation layer — no I/O, easily unit-testable.
"""

from app.config import settings
from app.models.score import AuctorScoreResponse


class ScoreService:

    def calculate(
        self,
        *,
        github_verified: bool = False,
        badges_earned: int = 0,
        projects_verified: int = 0,
        total_projects: int = 0,
        experience_verified: bool = False,
        leetcode_solved: int = 0,   # future: LeetCode integration
        leetcode_total: int = 300,  # arbitrary normalisation baseline
    ) -> AuctorScoreResponse:
        """
        Calculate the Auctor score from verification signals.

        All inputs are keyword-only to prevent positional-order bugs.
        """
        # ── Component fractions ───────────────────────────────────────────────
        github_frac = 1.0 if github_verified else 0.0

        leetcode_frac = min(leetcode_solved / leetcode_total, 1.0)

        # Each badge earned contributes 0.2 to the badge fraction (max 5 badges = 1.0)
        badges_frac = min(badges_earned * 0.2, 1.0)

        # Project fraction: ratio of verified projects to total
        if total_projects > 0:
            projects_frac = min(projects_verified / total_projects, 1.0)
        else:
            projects_frac = 0.0

        experience_frac = 1.0 if experience_verified else 0.0

        # ── Weighted sum → scale to 0–10 ─────────────────────────────────────
        weighted_sum = (
            github_frac     * settings.weight_github
            + leetcode_frac * settings.weight_leetcode
            + badges_frac   * settings.weight_badges
            + projects_frac * settings.weight_projects
            + experience_frac * settings.weight_experience
        )
        total = round(weighted_sum * 10, 2)

        return AuctorScoreResponse(
            total=total,
            github=round(github_frac, 4),
            leetcode=round(leetcode_frac, 4),
            badges=round(badges_frac, 4),
            projects=round(projects_frac, 4),
            experience=round(experience_frac, 4),
        )
