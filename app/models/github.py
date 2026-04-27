"""app/models/github.py — GitHub verification response schema."""

from pydantic import BaseModel, Field


class GitHubVerifyResponse(BaseModel):
    verified: bool
    username: str
    repos: int = Field(0, description="Total public repos")
    stars: int = Field(0, description="Total stars across all repos")
    matched_projects: list[str] = Field(
        default_factory=list,
        description="CV project names found in the user's GitHub repos",
    )
    profile_url: str = Field("", description="https://github.com/{username}")
