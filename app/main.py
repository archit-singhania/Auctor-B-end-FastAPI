"""
app/main.py — FastAPI application entry point.

Data flow (real mode):
  Flutter → POST /api/cv/parse       → pdfminer + OpenAI → INSERT cv_data
  Flutter → GET  /api/verify/github  → GitHub API        → UPSERT verifications
  Flutter → POST /api/badges/submit  → pass/fail logic   → UPSERT badges + scores
  Flutter → GET  /api/score          → SELECT scores      → return AuctorScore

Run:
  uvicorn app.main:app --reload --port 8000
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import create_pool, close_pool
from app.routers import cv, verify, score, badges


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open DB pool on startup, close it on shutdown."""
    await create_pool()
    yield
    await close_pool()


app = FastAPI(
    title="Auctor API",
    description="Developer Trust Score — CV parsing, GitHub verification, badge scoring.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Build origins list — always include wildcard so the deployed Vercel
# Flutter web build (which may run on any preview URL) isn't blocked.
_cors_origins = settings.cors_origins
if "*" not in _cors_origins:
    _cors_origins = _cors_origins + ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,   # must be False when allow_origins contains "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(cv.router,     prefix="/api/cv",     tags=["CV"])
app.include_router(verify.router, prefix="/api/verify", tags=["Verification"])
app.include_router(score.router,  prefix="/api",        tags=["Score"])
app.include_router(badges.router, prefix="/api/badges", tags=["Badges"])


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "auctor-api"}
