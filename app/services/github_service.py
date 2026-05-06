"""
app/services/github_service.py
────────────────────────────────
Hits the GitHub REST API v3 to:
  1. Confirm the user exists
  2. Collect public repo count + total stars
  3. Cross-match repo names against CV project names (fuzzy)

Authentication:
  Uses GITHUB_TOKEN from .env / Railway env vars as a Bearer token.
  Without a token, GitHub's rate limit is 60 req/hr AND unauthenticated
  requests to some endpoints return 401. Always set GITHUB_TOKEN.

  Create a token at: https://github.com/settings/tokens
  Required scope: public_repo (read-only classic token is sufficient)

Project matching algorithm:
  For each CV project name, we tokenise it and check whether any public
  repo name contains all tokens (case-insensitive). e.g.:
    CV project "Auth Microservice" → tokens ["auth", "microservice"]
    Repo "auth-microservice-jwt" → matches because both tokens present
"""

import logging

import httpx

from app.config import settings
from app.models.github import GitHubVerifyResponse

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubService:

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = settings.github_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning(
                "GITHUB_TOKEN is not set. Requests will be unauthenticated "
                "(60 req/hr limit). Some endpoints may return 401. "
                "Set GITHUB_TOKEN in Railway environment variables."
            )
        return headers

    async def verify(
        self, username: str, cv_projects: list[str]
    ) -> GitHubVerifyResponse:
        """
        Verify a GitHub username and cross-match repos against CV projects.

        Raises ValueError if user not found (404).
        Raises PermissionError if token is missing/invalid (401).
        """
        async with httpx.AsyncClient(headers=self._headers(), timeout=15) as client:
            # ── 1. Fetch user profile ─────────────────────────────────────────
            user_resp = await client.get(f"{GITHUB_API}/users/{username}")

            if user_resp.status_code == 401:
                raise PermissionError(
                    "GitHub API returned 401 Unauthorized. "
                    "Your GITHUB_TOKEN is missing, expired, or invalid. "
                    "Go to https://github.com/settings/tokens and create a new token, "
                    "then set GITHUB_TOKEN in your Railway environment variables."
                )
            if user_resp.status_code == 403:
                raise PermissionError(
                    "GitHub API returned 403 Forbidden. "
                    "You may have hit the rate limit (60 req/hr without a token). "
                    "Set a valid GITHUB_TOKEN in Railway to raise the limit to 5,000 req/hr."
                )
            if user_resp.status_code == 404:
                raise ValueError(f"GitHub user '{username}' not found")

            user_resp.raise_for_status()
            user_data = user_resp.json()

            public_repos: int = user_data.get("public_repos", 0)

            # ── 2. Fetch repos (up to 100, sorted by stars) ───────────────────
            repos_resp = await client.get(
                f"{GITHUB_API}/users/{username}/repos",
                params={"per_page": 100, "sort": "stars", "type": "public"},
            )
            repos_resp.raise_for_status()
            repos: list[dict] = repos_resp.json()

            total_stars: int = sum(r.get("stargazers_count", 0) for r in repos)
            repo_names: list[str] = [r.get("name", "").lower() for r in repos]

            # ── 3. Cross-match CV project names against repos ─────────────────
            matched: list[str] = []
            for project in cv_projects:
                if self._matches_any_repo(project, repo_names):
                    matched.append(project)

            # A user is "verified" if the GitHub account exists.
            # Extra credit if repos match CV projects.
            return GitHubVerifyResponse(
                verified=True,
                username=username,
                repos=public_repos,
                stars=total_stars,
                matched_projects=matched,
                profile_url=f"https://github.com/{username}",
            )

    @staticmethod
    def _matches_any_repo(project_name: str, repo_names: list[str]) -> bool:
        """
        Returns True if any repo name contains all tokens from the project name.
        e.g. "Auth Microservice" → tokens ["auth", "microservice"]
        """
        tokens = [t.lower() for t in project_name.split() if len(t) > 2]
        if not tokens:
            return False
        return any(all(tok in repo for tok in tokens) for repo in repo_names)
