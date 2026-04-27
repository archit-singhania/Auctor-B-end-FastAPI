"""
app/models/cv.py — Pydantic schemas for CV data.

These mirror the Dart models in auctor_models.dart exactly.
Field names use snake_case to match JSON serialisation on both sides.
"""

from pydantic import BaseModel, Field


class Project(BaseModel):
    name: str = Field(..., description="Project title as mentioned in the CV")
    description: str = Field("", description="One-line summary of what the project does")
    tech_stack: list[str] = Field(default_factory=list, description="Technologies used")
    is_verified: bool = Field(False, description="Set to True once GitHub cross-check passes")


class Experience(BaseModel):
    company: str = Field(..., description="Employer name")
    role: str = Field(..., description="Job title / internship role")
    duration: str = Field("", description="e.g. 'Jun 2023 – Dec 2023'")
    is_verified: bool = Field(False, description="Set to True when offer letter is verified")


class ExtractedCvData(BaseModel):
    """Full structured output of a CV parse operation."""
    skills: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
