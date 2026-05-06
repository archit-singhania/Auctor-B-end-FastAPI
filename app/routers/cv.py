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
import logging
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.db import get_conn, release_conn
from app.models.cv import ExtractedCvData
from app.services.cv_parser import CvParserService

router = APIRouter()
_parser = CvParserService()
logger = logging.getLogger(__name__)

# Accepted MIME types — Flutter/web file pickers can send a variety of these
_ACCEPTED_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "application/x-pdf",
    "binary/octet-stream",
    None,  # some multipart clients omit content-type entirely
}


@router.post("/parse", response_model=ExtractedCvData)
async def parse_cv(
    file: UploadFile = File(...),
    user_id: int = Query(1, description="User ID (defaults to demo user id=1)"),
) -> ExtractedCvData:
    """
    Upload a PDF CV → extract skills, projects, experience → save to DB.
    """
    # Accept any MIME that could be a PDF; validate magic bytes instead of MIME
    if file.content_type not in _ACCEPTED_TYPES:
        # Still allow if filename ends with .pdf — mobile pickers can lie about MIME
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, f"Expected a PDF file, got: {file.content_type}")

    pdf_bytes = await file.read()

    if len(pdf_bytes) == 0:
        raise HTTPException(400, "Uploaded file is empty.")

    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large. Max 10 MB.")

    # Validate PDF magic bytes (%PDF-)
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(400, "File does not appear to be a valid PDF.")

    try:
        data = await _parser.parse(pdf_bytes)
    except Exception as exc:
        logger.exception("CV parsing failed for user %s", user_id)
        raise HTTPException(500, f"CV parsing failed: {exc}") from exc

    # Persist to DB — wrapped in a transaction so delete+insert is atomic
    try:
        conn = await get_conn()
        try:
            async with conn.transaction():
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
    except Exception as db_exc:
        # Log DB failure but still return extracted data — don't fail the user
        logger.error("DB persist failed for user %s: %s", user_id, db_exc)

    return data
