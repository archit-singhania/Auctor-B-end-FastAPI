# Auctor FastAPI Backend

The verification and scoring engine for the Auctor Developer Trust Score platform.

---

## What this does

```
Flutter app → POST /api/cv/parse       → pdfminer extracts text → OpenAI GPT-4o-mini parses skills
Flutter app → GET  /api/verify/github  → GitHub REST API validates username + repos
Flutter app → POST /api/badges/submit  → evaluates quiz result, returns score delta
Flutter app → GET  /api/score          → calculates weighted Auctor Score (0–10)
```

---

## Quick start

### 1. Install Python 3.11+

### 2. Create a virtual environment

```bash
cd auctor_app_fast_api
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Then open `.env` and fill in:

| Variable | Where to get it | Required? |
|---|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | Optional (falls back to heuristic parser) |
| `GITHUB_TOKEN` | https://github.com/settings/tokens → Fine-grained PAT, public_repo read | Strongly recommended |

### 5. Run the server

```bash
uvicorn app.main:app --reload --port 8000
```

The API is now live at `http://localhost:8000`.

Interactive docs: http://localhost:8000/docs

---

## API Reference

### POST /api/cv/parse

Upload a PDF resume and receive structured data.

**Request:** `multipart/form-data` with a `file` field (PDF, max 10 MB)

**Response:**
```json
{
  "skills": ["JWT Authentication", "REST API", "Docker"],
  "projects": [
    {
      "name": "Auth Microservice",
      "description": "JWT-based auth service with refresh tokens",
      "tech_stack": ["JWT", "Node.js", "Redis"],
      "is_verified": false
    }
  ],
  "experience": [
    {
      "company": "TechCorp Pvt. Ltd.",
      "role": "Backend Developer Intern",
      "duration": "Jun 2023 – Dec 2023",
      "is_verified": false
    }
  ]
}
```

---

### GET /api/verify/github

Cross-check a GitHub username against CV projects.

**Query params:**
- `username` (required) — GitHub handle
- `cv_projects` (optional, repeatable) — project names from the CV

**Example:**
```
GET /api/verify/github?username=torvalds&cv_projects=Auth+Microservice
```

**Response:**
```json
{
  "verified": true,
  "username": "torvalds",
  "repos": 8,
  "stars": 1200,
  "matched_projects": ["Auth Microservice"],
  "profile_url": "https://github.com/torvalds"
}
```

---

### GET /api/score

Calculate the Auctor Score from verification signals.

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `github_verified` | bool | false | GitHub connected & verified |
| `badges_earned` | int | 0 | Number of badges passed |
| `projects_verified` | int | 0 | Verified project count |
| `total_projects` | int | 0 | Total projects in CV |
| `experience_verified` | bool | false | Work experience proof uploaded |

**Response:**
```json
{
  "total": 4.2,
  "github": 0.65,
  "leetcode": 0.0,
  "badges": 0.1,
  "projects": 0.2,
  "experience": 0.0
}
```

---

### POST /api/badges/submit

Submit badge challenge result.

**Body:**
```json
{
  "badge_id": "jwt-auth",
  "correct_answers": 4,
  "total_questions": 5
}
```

**Response:**
```json
{
  "passed": true,
  "badge_id": "jwt-auth",
  "score_gained": 0.8
}
```

---

## Score Weights

| Component | Weight | How fraction is calculated |
|---|---|---|
| GitHub | 25% | 1.0 if verified, 0.0 otherwise |
| LeetCode | 15% | solved / 300 (capped at 1.0) |
| Badges | 30% | earned × 0.2 (max 5 badges = 1.0) |
| Projects | 15% | verified / total |
| Experience | 15% | 1.0 if offer letter uploaded, 0.0 otherwise |

Change weights in `.env` — they must sum to 1.0.

---

## Connecting to the Flutter app

1. Start the server: `uvicorn app.main:app --port 8000`
2. In `auctor_app_flutter/lib/core/constants/app_constants.dart`:
   - Android emulator: `apiBaseUrl = 'http://10.0.2.2:8000'` (already set)
   - Physical device on LAN: `apiBaseUrl = 'http://192.168.x.x:8000'`
   - Set `useMockData = false`
3. Run `flutter pub get` then `flutter run`

---

## Project structure

```
auctor_app_fast_api/
├── app/
│   ├── main.py          ← FastAPI app, CORS, router mounts
│   ├── config.py        ← Pydantic settings (reads .env)
│   ├── models/
│   │   ├── cv.py        ← ExtractedCvData, Project, Experience schemas
│   │   ├── github.py    ← GitHubVerifyResponse schema
│   │   ├── score.py     ← AuctorScoreResponse schema
│   │   └── badge.py     ← BadgeSubmitRequest/Response schemas
│   ├── routers/
│   │   ├── cv.py        ← POST /api/cv/parse
│   │   ├── verify.py    ← GET  /api/verify/github
│   │   ├── score.py     ← GET  /api/score
│   │   └── badges.py    ← POST /api/badges/submit
│   └── services/
│       ├── cv_parser.py      ← PDF text extraction + LLM/heuristic parsing
│       ├── github_service.py ← GitHub API calls + fuzzy project matching
│       └── score_service.py  ← Pure score calculation (no I/O)
├── requirements.txt
├── .env.example
└── README.md
```
# Auctor-B-end-FastAPI
