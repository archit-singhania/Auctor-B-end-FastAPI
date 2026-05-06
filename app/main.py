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
# Explicitly list all allowed origins so the browser never gets a mismatch.
# Wildcard "*" can cause issues with some browser XHR implementations.
# Add new Vercel preview URLs here if needed.
ALLOWED_ORIGINS = [
    "https://auctor-flutter-init.vercel.app",   # production Vercel
    "https://auctor-f-end-flutter.vercel.app",  # alternate Vercel URL
    "http://localhost:3000",                     # local web dev
    "http://localhost:8080",
    "http://localhost:5000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://auctor.*\.vercel\.app",  # all Vercel preview deployments
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["Content-Type", "X-Request-ID"],
    max_age=86400,
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(cv.router,     prefix="/api/cv",     tags=["CV"])
app.include_router(verify.router, prefix="/api/verify", tags=["Verification"])
app.include_router(score.router,  prefix="/api",        tags=["Score"])
app.include_router(badges.router, prefix="/api/badges", tags=["Badges"])


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "auctor-api"}


@app.options("/{rest_of_path:path}", include_in_schema=False)
async def preflight_handler(rest_of_path: str) -> dict:
    """Catch-all OPTIONS handler so every endpoint responds 200 to preflight."""
    return {}
