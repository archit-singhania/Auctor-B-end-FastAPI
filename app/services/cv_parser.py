"""
app/services/cv_parser.py
──────────────────────────
Extracts structured CV data from raw PDF bytes.

Two-step process:
  1. Text extraction  — pdfminer.six pulls plain text from the PDF
  2. Structured parse — OpenAI GPT-4o parses skills/projects/experience
                        (falls back to regex heuristics if API key is absent)
"""

import io
import json
import re
import logging

from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams

from app.config import settings
from app.models.cv import ExtractedCvData, Experience, Project

logger = logging.getLogger(__name__)


class CvParserService:
    """Parses a PDF resume into structured ExtractedCvData."""

    # ── Public API ────────────────────────────────────────────────────────────

    async def parse(self, pdf_bytes: bytes) -> ExtractedCvData:
        """Main entry point. Accepts raw PDF bytes, returns ExtractedCvData."""
        raw_text = self._extract_text(pdf_bytes)

        if not raw_text or not raw_text.strip():
            raise ValueError(
                "Could not extract text from PDF. "
                "The file may be scanned/image-only or password-protected."
            )

        logger.info("Extracted %d chars from PDF", len(raw_text))

        if settings.openai_api_key and settings.openai_api_key.strip():
            try:
                return await self._llm_parse(raw_text)
            except Exception as exc:
                logger.warning("LLM parse failed, falling back to heuristic: %s", exc)

        logger.info("Using heuristic CV parser (no OpenAI key configured)")
        return self._heuristic_parse(raw_text)

    # ── Text Extraction ───────────────────────────────────────────────────────

    def _extract_text(self, pdf_bytes: bytes) -> str:
        """Use pdfminer to extract plain text from a PDF byte string."""
        output = io.StringIO()
        try:
            with io.BytesIO(pdf_bytes) as pdf_file:
                extract_text_to_fp(
                    pdf_file,
                    output,
                    laparams=LAParams(),
                    output_type="text",
                    codec="utf-8",
                )
        except Exception as exc:
            logger.error("pdfminer extraction failed: %s", exc)
            raise ValueError(f"PDF text extraction failed: {exc}") from exc
        return output.getvalue()

    # ── LLM Parse (OpenAI) ────────────────────────────────────────────────────

    async def _llm_parse(self, raw_text: str) -> ExtractedCvData:
        """Send resume text to OpenAI and ask for structured JSON output."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)

        prompt = f"""
You are an expert resume parser. Extract structured information from this resume text.

Return ONLY a JSON object — no preamble, no markdown fences, no explanation.
The JSON must have exactly this structure:

{{
  "skills": ["skill1", "skill2", ...],
  "projects": [
    {{
      "name": "Project Name",
      "description": "One-sentence description of what it does",
      "tech_stack": ["tech1", "tech2"]
    }}
  ],
  "experience": [
    {{
      "company": "Company Name",
      "role": "Job Title",
      "duration": "Month Year – Month Year"
    }}
  ]
}}

Rules:
- skills: extract technical skills only (languages, frameworks, tools, databases)
- projects: only named projects with tech context, max 5
- experience: only real employment/internships, max 5
- all string values must be non-empty

Resume text:
{raw_text[:6000]}
"""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=1500,
        )

        raw_json = response.choices[0].message.content or "{}"
        raw_json = re.sub(r"```json|```", "", raw_json).strip()

        data = json.loads(raw_json)
        return ExtractedCvData(
            skills=data.get("skills", []),
            projects=[
                Project(
                    name=p.get("name", "Unnamed Project"),
                    description=p.get("description", ""),
                    tech_stack=p.get("tech_stack", []),
                )
                for p in data.get("projects", [])
                if p.get("name")
            ],
            experience=[
                Experience(
                    company=e.get("company", "Unknown"),
                    role=e.get("role", "Unknown"),
                    duration=e.get("duration", ""),
                )
                for e in data.get("experience", [])
                if e.get("company") and e.get("role")
            ],
        )

    # ── Heuristic Parse (no API key) ──────────────────────────────────────────

    def _heuristic_parse(self, raw_text: str) -> ExtractedCvData:
        """
        Regex-based fallback. Less accurate than LLM but zero dependencies.
        Looks for common CV section headers and extracts bullet-point items.
        """
        skills = self._extract_skills_heuristic(raw_text)
        projects = self._extract_projects_heuristic(raw_text)
        experience = self._extract_experience_heuristic(raw_text)

        # Ensure we always return something useful even with a sparse CV
        if not skills:
            logger.warning("Heuristic found no skills — returning defaults")
            skills = ["See CV for details"]

        return ExtractedCvData(skills=skills, projects=projects, experience=experience)

    # Common tech keywords to detect as skills
    _TECH_KEYWORDS = [
        "Python", "JavaScript", "TypeScript", "Java", "Kotlin", "Swift",
        "Flutter", "Dart", "React", "Vue", "Angular", "Node.js", "FastAPI",
        "Django", "Flask", "Spring", "Docker", "Kubernetes", "AWS", "GCP",
        "Azure", "PostgreSQL", "MySQL", "MongoDB", "Redis", "GraphQL",
        "REST", "JWT", "OAuth", "Git", "Linux", "CI/CD", "Terraform",
        "Microservices", "gRPC", "Kafka", "RabbitMQ", "C++", "C#", "Go",
        "Rust", "Ruby", "PHP", "HTML", "CSS", "Sass", "Tailwind",
        "TensorFlow", "PyTorch", "Pandas", "NumPy", "Scikit-learn",
        "Firebase", "Supabase", "Prisma", "Next.js", "Nuxt", "Express",
        "Nest.js", "Spring Boot", "Hibernate", "Maven", "Gradle",
        "Jenkins", "GitHub Actions", "CircleCI", "Ansible", "Nginx",
    ]

    def _extract_skills_heuristic(self, text: str) -> list[str]:
        found = []
        for kw in self._TECH_KEYWORDS:
            if re.search(rf"\b{re.escape(kw)}\b", text, re.IGNORECASE):
                found.append(kw)
        return found

    def _extract_projects_heuristic(self, text: str) -> list[Project]:
        """Look for 'Projects' section and extract named items."""
        projects: list[Project] = []
        section = self._extract_section(text, "projects")
        if not section:
            return projects

        for line in section.splitlines():
            line = line.strip()
            if len(line) > 5 and not line.lower().startswith(("•", "-", "*", "·")):
                projects.append(
                    Project(
                        name=line[:80],
                        description="",
                        tech_stack=[],
                    )
                )
            if len(projects) >= 5:
                break
        return projects

    def _extract_experience_heuristic(self, text: str) -> list[Experience]:
        experiences: list[Experience] = []
        section = self._extract_section(text, "experience") or self._extract_section(
            text, "work"
        )
        if not section:
            return experiences

        pattern = re.compile(
            r"(.+?)\s+(?:at|@)\s+(.+?)[\s·|,]+(\w{3}\s+\d{4}.*)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(section):
            experiences.append(
                Experience(
                    role=match.group(1).strip()[:60],
                    company=match.group(2).strip()[:60],
                    duration=match.group(3).strip()[:40],
                )
            )
            if len(experiences) >= 5:
                break
        return experiences

    def _extract_section(self, text: str, section_name: str) -> str | None:
        """Extract text between a section header and the next header."""
        pattern = re.compile(
            rf"^{section_name}\s*\n(.*?)(?=\n[A-Z][A-Za-z ]+\n|\Z)",
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        match = pattern.search(text)
        return match.group(1) if match else None
