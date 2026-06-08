"""
app/services/cv_parser.py
--------------------------
Extracts structured CV data from raw PDF bytes.

Two-step process:
  1. Text extraction  - pdfminer.six pulls plain text from the PDF
  2. Structured parse - OpenAI GPT-4o parses skills/projects/experience/profiles
                        (falls back to regex heuristics if API key is absent)

NEW: Also extracts profile links — GitHub, LinkedIn, LeetCode, GeeksForGeeks,
     email, phone, portfolio URL — from both the raw text and embedded URLs.
"""

import io
import json
import re
import logging

from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams

from app.config import settings
from app.models.cv import ExtractedCvData, ExtractedProfiles, Experience, Project

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

        # Always extract profiles via regex — fast and reliable
        profiles = self._extract_profiles(raw_text)

        if settings.openai_api_key and settings.openai_api_key.strip():
            try:
                data = await self._llm_parse(raw_text)
                # Merge profiles — LLM may also pick some up, but regex is more reliable
                data = ExtractedCvData(
                    skills=data.skills,
                    projects=data.projects,
                    experience=data.experience,
                    profiles=self._merge_profiles(profiles, data.profiles),
                )
                return data
            except Exception as exc:
                logger.warning("LLM parse failed, falling back to heuristic: %s", exc)

        logger.info("Using heuristic CV parser (no OpenAI key configured)")
        data = self._heuristic_parse(raw_text)
        return ExtractedCvData(
            skills=data.skills,
            projects=data.projects,
            experience=data.experience,
            profiles=profiles,
        )

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

    # -- Profile Extraction ---------------------------------------------------

    def _extract_profiles(self, text: str) -> ExtractedProfiles:
        """
        Extract all external profile links and contact info from raw CV text.
        Uses targeted regexes for each platform. Normalises to just the username
        slug so the frontend can build deep-links.
        """
        # ── Email ──────────────────────────────────────────────────────────
        email = ""
        email_match = re.search(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text
        )
        if email_match:
            email = email_match.group(0).strip()

        # ── Phone ──────────────────────────────────────────────────────────
        phone = ""
        phone_match = re.search(
            r"(?:\+?\d{1,3}[\s\-.]?)?"          # optional country code
            r"(?:\(?\d{2,4}\)?[\s\-.]?)"         # area code
            r"\d{3,4}[\s\-.]?\d{4}",             # main number
            text
        )
        if phone_match:
            phone = phone_match.group(0).strip()

        # ── GitHub ─────────────────────────────────────────────────────────
        github = ""
        github_match = re.search(
            r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_\-]+)(?:/[^\s]*)?",
            text, re.IGNORECASE
        )
        if github_match:
            github = github_match.group(1)
        else:
            # Bare username after "GitHub:" or "github:" label
            gh_label = re.search(
                r"(?i)github\s*[:\-|]?\s*@?([A-Za-z0-9_\-]{2,39})\b", text
            )
            if gh_label:
                username = gh_label.group(1)
                # Ignore if it looks like a noise word
                if username.lower() not in {"com", "io", "profile", "username", "user"}:
                    github = username

        # ── LinkedIn ───────────────────────────────────────────────────────
        linkedin = ""
        li_match = re.search(
            r"(?:https?://)?(?:www\.)?linkedin\.com/in/([A-Za-z0-9_\-]+)(?:[/?][^\s]*)?",
            text, re.IGNORECASE
        )
        if li_match:
            linkedin = li_match.group(1)
        else:
            li_label = re.search(
                r"(?i)linkedin\s*[:\-|]?\s*(?:in/)?@?([A-Za-z0-9_\-]{2,60})\b", text
            )
            if li_label:
                candidate = li_label.group(1)
                if candidate.lower() not in {"com", "profile", "username", "user", "www"}:
                    linkedin = candidate

        # ── LeetCode ───────────────────────────────────────────────────────
        leetcode = ""
        lc_match = re.search(
            r"(?:https?://)?(?:www\.)?leetcode\.com/(?:u/)?([A-Za-z0-9_\-]+)(?:[/?][^\s]*)?",
            text, re.IGNORECASE
        )
        if lc_match:
            leetcode = lc_match.group(1)
        else:
            lc_label = re.search(
                r"(?i)leetcode\s*[:\-|]?\s*@?([A-Za-z0-9_\-]{2,39})\b", text
            )
            if lc_label:
                candidate = lc_label.group(1)
                if candidate.lower() not in {"com", "profile", "username", "user"}:
                    leetcode = candidate

        # ── GeeksForGeeks ──────────────────────────────────────────────────
        gfg = ""
        gfg_match = re.search(
            r"(?:https?://)?(?:www\.)?geeksforgeeks\.org/user/([A-Za-z0-9_\-]+)(?:[/?][^\s]*)?",
            text, re.IGNORECASE
        )
        if gfg_match:
            gfg = gfg_match.group(1)
        else:
            gfg_label = re.search(
                r"(?i)geeks\s*for\s*geeks\s*[:\-|]?\s*@?([A-Za-z0-9_\-]{2,50})\b",
                text
            )
            if gfg_label:
                candidate = gfg_label.group(1)
                if candidate.lower() not in {"com", "profile", "username", "user"}:
                    gfg = candidate

        # ── Portfolio / Personal Website ────────────────────────────────────
        portfolio = ""
        # Match any URL that is NOT github/linkedin/leetcode/gfg/twitter
        portfolio_match = re.search(
            r"https?://(?!(?:www\.)?"
            r"(?:github\.com|linkedin\.com|leetcode\.com|geeksforgeeks\.org|twitter\.com|x\.com))"
            r"[A-Za-z0-9.\-/]+\.[a-zA-Z]{2,}(?:/[^\s]*)?",
            text, re.IGNORECASE
        )
        if portfolio_match:
            portfolio = portfolio_match.group(0).rstrip(".,;)")

        # ── Twitter / X ────────────────────────────────────────────────────
        twitter = ""
        tw_match = re.search(
            r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]+)(?:[/?][^\s]*)?",
            text, re.IGNORECASE
        )
        if tw_match:
            twitter = tw_match.group(1)
        else:
            tw_label = re.search(
                r"(?i)(?:twitter|x)\s*[:\-|]?\s*@([A-Za-z0-9_]{1,15})\b", text
            )
            if tw_label:
                twitter = tw_label.group(1)

        return ExtractedProfiles(
            email=email,
            phone=phone,
            github=github,
            linkedin=linkedin,
            leetcode=leetcode,
            geeksforgeeks=gfg,
            portfolio=portfolio,
            twitter=twitter,
        )

    def _merge_profiles(
        self, regex: ExtractedProfiles, llm: ExtractedProfiles
    ) -> ExtractedProfiles:
        """Prefer non-empty value; regex wins for structured fields like email/phone."""
        def pick(r: str, l: str) -> str:
            return r if r else l

        return ExtractedProfiles(
            email=pick(regex.email, llm.email),
            phone=pick(regex.phone, llm.phone),
            github=pick(regex.github, llm.github),
            linkedin=pick(regex.linkedin, llm.linkedin),
            leetcode=pick(regex.leetcode, llm.leetcode),
            geeksforgeeks=pick(regex.geeksforgeeks, llm.geeksforgeeks),
            portfolio=pick(regex.portfolio, llm.portfolio),
            twitter=pick(regex.twitter, llm.twitter),
        )

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
  ],
  "profiles": {{
    "email": "",
    "phone": "",
    "github": "",
    "linkedin": "",
    "leetcode": "",
    "geeksforgeeks": "",
    "portfolio": "",
    "twitter": ""
  }}
}}

Rules:
- skills: technical skills only (languages, frameworks, tools, databases)
- projects: only named projects with tech context, max 6
- experience: only real employment/internships, max 5
- profiles: extract usernames only (not full URLs) for github/linkedin/leetcode/gfg/twitter
- profiles.email and profiles.phone: exact as found in the text
- all string values must be non-empty or empty string ""
- IMPORTANT for projects: each project must have a distinct proper name (e.g. "AccessHub", "HealthSync").
  Do NOT split a single project description across multiple entries.
  Do NOT use a sentence fragment as a project name.

Resume text:
{raw_text[:6000]}
"""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=1800,
        )

        raw_json = response.choices[0].message.content or "{}"
        raw_json = re.sub(r"```json|```", "", raw_json).strip()

        data = json.loads(raw_json)
        profiles_raw = data.get("profiles", {})

        projects_raw = data.get("projects", [])
        # Filter out entries whose name looks like a sentence fragment
        # (proper project names are typically short — under 8 words — and
        # don't start with a lowercase letter or a verb fragment)
        def _is_valid_project_name(name: str) -> bool:
            if not name:
                return False
            words = name.strip().split()
            if len(words) > 10:
                return False  # almost certainly a sentence fragment
            # Reject if it starts with a verb fragment pattern common in bullet points
            if re.match(r'^(Designed|Developed|Built|Created|Implemented|Integrated|'
                        r'Engineered|Architected|Improved|Optimized|Used|Leveraged)\b',
                        name, re.IGNORECASE):
                return False
            return True

        return ExtractedCvData(
            skills=data.get("skills", []),
            projects=[
                Project(
                    name=p.get("name", "Unnamed Project"),
                    description=p.get("description", ""),
                    tech_stack=p.get("tech_stack", []),
                )
                for p in projects_raw
                if p.get("name") and _is_valid_project_name(p.get("name", ""))
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
            profiles=ExtractedProfiles(
                email=profiles_raw.get("email", ""),
                phone=profiles_raw.get("phone", ""),
                github=profiles_raw.get("github", ""),
                linkedin=profiles_raw.get("linkedin", ""),
                leetcode=profiles_raw.get("leetcode", ""),
                geeksforgeeks=profiles_raw.get("geeksforgeeks", ""),
                portfolio=profiles_raw.get("portfolio", ""),
                twitter=profiles_raw.get("twitter", ""),
            ),
        )

    # -- Heuristic Parse (no API key) -----------------------------------------

    def _heuristic_parse(self, raw_text: str) -> ExtractedCvData:
        skills = self._extract_skills_heuristic(raw_text)
        projects = self._extract_projects_heuristic(raw_text)
        experience = self._extract_experience_heuristic(raw_text)

        if not skills:
            logger.warning("Heuristic found no skills -- returning defaults")
            skills = ["See CV for details"]

        return ExtractedCvData(
            skills=skills,
            projects=projects,
            experience=experience,
            profiles=ExtractedProfiles(),  # profiles always extracted separately
        )

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

    # Project name patterns — lines that look like proper project titles
    # (short, title-case, optionally with a dash/subtitle)
    _PROJECT_TITLE_RE = re.compile(
        r'^([A-Z][A-Za-z0-9]+(?:[\s\-–]+[A-Za-z0-9]+){0,6})$'
    )

    def _extract_projects_heuristic(self, text: str) -> list[Project]:
        """
        Heuristic project extractor.

        Strategy: find the PROJECTS section, then identify project titles as
        short title-case lines that are followed by description/tech lines.
        Group continuation lines (description + tech bullets) under the same
        project until the next title-like line appears.
        """
        projects: list[Project] = []
        section = self._extract_section(text, r"(?:university\s+&?\s+personal\s+)?projects?")
        if not section:
            # Try broader search in the full text for named project blocks
            section = self._extract_section(text, r"projects?")
        if not section:
            return projects

        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
        if not lines:
            return projects

        def _is_project_title(line: str) -> bool:
            """A project title is short, starts with a capital, and is NOT a sentence."""
            if len(line) > 90:
                return False
            word_count = len(line.split())
            if word_count > 9:
                return False
            # Must start with a capital letter
            if not line[0].isupper():
                return False
            # Reject if it starts with a common description verb
            if re.match(
                r'^(Designed|Developed|Built|Created|Implemented|Integrated|'
                r'Engineered|Architected|Improved|Optimized|Used|Leveraged|'
                r'Currently|Took|Spent|Collaborated|Worked)\b',
                line, re.IGNORECASE
            ):
                return False
            # Reject date-only or company-only lines
            if re.match(r'^\d{4}', line):
                return False
            return True

        # Group lines into (title, [body_lines]) chunks
        chunks: list[tuple[str, list[str]]] = []
        current_title: str | None = None
        current_body: list[str] = []

        for line in lines:
            if _is_project_title(line):
                if current_title:
                    chunks.append((current_title, current_body))
                current_title = line
                current_body = []
            else:
                if current_title:
                    current_body.append(line)
                # else: preamble noise before first title — skip

        if current_title:
            chunks.append((current_title, current_body))

        for title, body_lines in chunks:
            if len(projects) >= 6:
                break
            full_context = title + " " + " ".join(body_lines)
            tech: list[str] = []
            for kw in self._TECH_KEYWORDS:
                if re.search(rf"\b{re.escape(kw)}\b", full_context, re.IGNORECASE):
                    tech.append(kw)

            # Use the first body line (if any) as description, truncated
            desc = ""
            for bl in body_lines:
                if len(bl) > 20:
                    desc = bl[:160]
                    break

            projects.append(Project(
                name=title[:80],
                description=desc,
                tech_stack=tech,
            ))

        return projects

    def _extract_experience_heuristic(self, text: str) -> list[Experience]:
        experiences: list[Experience] = []
        section = (
            self._extract_section(text, r"(?:professional\s+)?(?:work\s+)?experience")
            or self._extract_section(text, r"employment")
            or self._extract_section(text, r"work history")
        )
        if not section:
            return experiences

        # ── Strategy: find lines with a date range; treat them as job entries ──
        date_re = re.compile(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?"
            r"\s*\d{4}\s*[-–]\s*"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?\s*"
            r"(?:\d{4}|Present|present|current|Current))",
            re.IGNORECASE,
        )

        lines = [ln.strip() for ln in section.splitlines() if ln.strip()]

        # First pass: find all lines that contain a date range — those are job headers
        job_header_indices = [
            i for i, ln in enumerate(lines) if date_re.search(ln)
        ]

        if job_header_indices:
            for idx in job_header_indices:
                if len(experiences) >= 5:
                    break
                header = lines[idx]
                date_match = date_re.search(header)
                duration = date_match.group(1).strip() if date_match else ""

                # Remove date from header to get company/role text
                header_clean = date_re.sub("", header).strip().strip("|-,.").strip()

                # Try to split "Role at Company" or "Role — Company" or "Role | Company"
                sep = re.split(r'\s+(?:at|@|—|–|\|)\s+', header_clean, maxsplit=1)
                if len(sep) == 2:
                    role, company = sep[0].strip()[:80], sep[1].strip()[:80]
                else:
                    # Company is likely on the previous line
                    role = header_clean[:80]
                    company = lines[idx - 1][:80] if idx > 0 else "Unknown"

                if role and len(role) > 2:
                    experiences.append(Experience(
                        company=company or "Unknown",
                        role=role,
                        duration=duration,
                    ))
        else:
            # Fallback: original line-by-line approach
            i = 0
            while i < len(lines) and len(experiences) < 5:
                line = lines[i]
                date_match = date_re.search(line)
                duration = date_match.group(1).strip() if date_match else ""
                clean = date_re.sub("", line).strip().strip("|-. ").strip()

                if not clean and i + 1 < len(lines):
                    i += 1
                    clean = lines[i]

                sep = re.split(r"\s+(?:at|@|,|\|)\s+", clean, maxsplit=1)
                if len(sep) == 2:
                    role, company = sep[0].strip(), sep[1].strip()
                else:
                    role = clean[:60]
                    company = ""
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
        pattern = re.compile(
            rf"^\s*{section_name}\s*:?\s*\n(.*?)(?=\n\s*[A-Z][A-Za-z ]+\s*:?\s*\n|\Z)",
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        match = pattern.search(text)
        if match:
            return match.group(1)

        section_header_re = re.compile(r'^[A-Z][A-Za-z ]+:?$')
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if re.search(rf"\b{section_name}\b", line, re.IGNORECASE) and len(line.strip()) < 50:
                section_lines = []
                for j in range(i + 1, min(i + 60, len(lines))):
                    next_line = lines[j]
                    stripped = next_line.strip()
                    if stripped and len(stripped) < 40 and section_header_re.match(stripped):
                        break
                    section_lines.append(next_line)
                if section_lines:
                    return "\n".join(section_lines)
        return None
