"""
app/services/cv_parser.py
--------------------------
Extracts structured CV data from raw PDF bytes.

Two-step process:
  1. Text extraction  - pdfminer.six pulls plain text from the PDF
  2. Structured parse - OpenAI GPT-4o parses skills/projects/experience
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

    # -- Public API ------------------------------------------------------------

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

    # -- Text Extraction -------------------------------------------------------

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

    # -- LLM Parse (OpenAI) ---------------------------------------------------

    async def _llm_parse(self, raw_text: str) -> ExtractedCvData:
        """Send resume text to OpenAI and ask for structured JSON output."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)

        prompt = f"""
You are an expert resume parser. Extract structured information from this resume text.

Return ONLY a JSON object - no preamble, no markdown fences, no explanation.
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
      "duration": "Month Year - Month Year"
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

    # -- Heuristic Parse (no API key) -----------------------------------------

    def _heuristic_parse(self, raw_text: str) -> ExtractedCvData:
        """
        Regex-based fallback. Less accurate than LLM but zero dependencies.
        Looks for common CV section headers and extracts bullet-point items.
        """
        skills = self._extract_skills_heuristic(raw_text)
        projects = self._extract_projects_heuristic(raw_text)
        experience = self._extract_experience_heuristic(raw_text)

        if not skills:
            logger.warning("Heuristic found no skills -- returning defaults")
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
        """Look for Projects section and extract named items with tech stack."""
        projects: list[Project] = []
        section = self._extract_section(text, r"projects?")
        if not section:
            return projects

        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
        i = 0
        while i < len(lines) and len(projects) < 5:
            line = lines[i]
            # Skip very short lines (less than 5 chars) - likely noise
            if len(line) < 5:
                i += 1
                continue
            # Strip leading bullet/number markers
            clean = re.sub(r'^[\-\*>\d\.\|]+\s*', '', line).strip()
            if len(clean) > 8:
                # Detect tech keywords in this line and the next 2 lines for context
                tech: list[str] = []
                context = clean
                for k in range(1, 3):
                    if i + k < len(lines):
                        context += ' ' + lines[i + k]
                for kw in self._TECH_KEYWORDS:
                    if re.search(rf"\b{re.escape(kw)}\b", context, re.IGNORECASE):
                        tech.append(kw)
                # Get description from next line if it looks like a sentence
                desc = ""
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if len(next_line) > 15 and not re.match(r'^[\-\*>\d\.]+', next_line):
                        desc = next_line[:120]
                projects.append(Project(name=clean[:80], description=desc, tech_stack=tech))
            i += 1
        return projects

    def _extract_experience_heuristic(self, text: str) -> list[Experience]:
        """Extract employment entries by detecting date ranges and role/company patterns."""
        experiences: list[Experience] = []
        section = (
            self._extract_section(text, r"(?:work\s+)?experience")
            or self._extract_section(text, r"employment")
            or self._extract_section(text, r"work history")
        )
        if not section:
            return experiences

        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]

        # Matches: "Jun 2023 - Dec 2023", "2022 - Present", "2021-2023"
        date_re = re.compile(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?"
            r"\s*\d{4}\s*[-]\s*"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?\s*"
            r"(?:\d{4}|Present|present|current|Current))",
            re.IGNORECASE,
        )

        i = 0
        while i < len(lines) and len(experiences) < 5:
            line = lines[i]
            date_match = date_re.search(line)
            duration = date_match.group(1).strip() if date_match else ""
            # Remove date from line to isolate role/company text
            clean = date_re.sub("", line).strip().strip("|-. ").strip()

            if not clean and i + 1 < len(lines):
                i += 1
                clean = lines[i]

            # Try splitting "Role at Company" or "Role | Company" or "Role, Company"
            sep = re.split(r"\s+(?:at|@|,|\|)\s+", clean, maxsplit=1)
            if len(sep) == 2:
                role, company = sep[0].strip(), sep[1].strip()
            else:
                role = clean[:60]
                company = ""
                # Try next line as company name
                if i + 1 < len(lines):
                    nc = date_re.sub("", lines[i + 1]).strip()
                    if nc and not date_re.search(lines[i + 1]):
                        company = nc[:60]
                        i += 1

            if role and len(role) > 3:
                experiences.append(
                    Experience(company=company or "Unknown", role=role, duration=duration)
                )
            i += 1

        return experiences

    def _extract_section(self, text: str, section_name: str) -> str | None:
        """Extract text between a section header and the next section header."""
        # Primary: regex approach — permissive, handles indented headers and colons
        pattern = re.compile(
            rf"^\s*{section_name}\s*:?\s*\n(.*?)(?=\n\s*[A-Z][A-Za-z ]+\s*:?\s*\n|\Z)",
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        match = pattern.search(text)
        if match:
            return match.group(1)

        # Fallback: line-by-line scan for section header keyword
        section_header_re = re.compile(r'^[A-Z][A-Za-z ]+:?$')
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(rf"\b{section_name}\b", line, re.IGNORECASE) and len(line.strip()) < 40:
                section_lines = []
                for j in range(i + 1, min(i + 41, len(lines))):
                    next_line = lines[j]
                    stripped = next_line.strip()
                    # Stop at next major section header
                    if stripped and len(stripped) < 35 and section_header_re.match(stripped):
                        break
                    section_lines.append(next_line)
                if section_lines:
                    return "\n".join(section_lines)
        return None
