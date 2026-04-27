"""
app/routers/cv.py

POST /api/cv/parse?user_id={uid}
─────────────────────────────────
1. Receive PDF bytes
2. Extract text (pdfminer)
3. Parse skills/projects/experience (OpenAI or heuristic)
4. UPSERT cv_data table for this user
5. Return ExtractedCvData
"""

import json
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.db import get_conn, release_conn
from app.models.cv import ExtractedCvData
from app.services.cv_parser import CvParserService

router = APIRouter()
_parser = CvParserService()


@router.post("/parse", response_model=ExtractedCvData)
async def parse_cv(
    file: UploadFile = File(...),
    user_id: int = Query(1, description="User ID (defaults to demo user id=1)"),
) -> ExtractedCvData:
    """
    Upload a PDF CV → extract skills, projects, experience → save to DB.
    """
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(400, f"Expected PDF, got: {file.content_type}")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large. Max 10 MB.")

    try:
        data = await _parser.parse(pdf_bytes)
    except Exception as exc:
        raise HTTPException(500, f"CV parsing failed: {exc}") from exc

    # ── Persist to DB ──────────────────────────────────────────────────────
    conn = await get_conn()
    try:
        # Delete old CV data for this user and insert fresh
        await conn.execute("DELETE FROM cv_data WHERE user_id = $1", user_id)
        await conn.execute(
            """
            INSERT INTO cv_data (user_id, skills, projects, experience)
            VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb)
            """,
            user_id,
            json.dumps(data.skills),
            json.dumps([p.model_dump() for p in data.projects]),
            json.dumps([e.model_dump() for e in data.experience]),
        )
    finally:
        await release_conn(conn)

    return data
