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

import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

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


# ── Custom CORS middleware (replaces CORSMiddleware entirely) ─────────────────
# CORSMiddleware has edge cases with multipart preflight + wildcard origins.
# This custom middleware is explicit and handles every case correctly.
class PermissiveCORSMiddleware(BaseHTTPMiddleware):
    # Regex: allow any Vercel preview + localhost for dev
    _ORIGIN_RE = re.compile(
        r"^https://[\w-]+\.vercel\.app$"
        r"|^http://localhost(:\d+)?$"
        r"|^http://127\.0\.0\.1(:\d+)?$"
    )

    CORS_HEADERS = {
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "Content-Type, X-Request-ID",
        "Access-Control-Max-Age": "86400",
    }

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # Decide which origin to echo back
        if self._ORIGIN_RE.match(origin):
            allow_origin = origin   # echo the exact origin back
        else:
            allow_origin = "*"      # non-browser or unknown origin — use wildcard

        # Short-circuit OPTIONS (preflight) — never forward to app
        if request.method == "OPTIONS":
            headers = {**self.CORS_HEADERS, "Access-Control-Allow-Origin": allow_origin}
            return Response(status_code=200, headers=headers)

        # Normal request — call the route, then inject CORS headers
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        for k, v in self.CORS_HEADERS.items():
            response.headers[k] = v
        return response


app.add_middleware(PermissiveCORSMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(cv.router,     prefix="/api/cv",     tags=["CV"])
app.include_router(verify.router, prefix="/api/verify", tags=["Verification"])
app.include_router(score.router,  prefix="/api",        tags=["Score"])
app.include_router(badges.router, prefix="/api/badges", tags=["Badges"])


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "auctor-api"}
