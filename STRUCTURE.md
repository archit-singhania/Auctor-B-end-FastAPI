auctor_fastapi/
├── app/
│   ├── __init__.py
│   ├── main.py                  ← FastAPI app entry point, CORS, router mounts
│   ├── config.py                ← Settings (env vars, GitHub token, etc.)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── cv.py                ← Pydantic schemas for CV data
│   │   ├── score.py             ← Pydantic schemas for AuctorScore
│   │   ├── badge.py             ← Pydantic schemas for Badge submission
│   │   └── github.py            ← Pydantic schemas for GitHub verify
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── cv.py                ← POST /api/cv/parse
│   │   ├── verify.py            ← GET /api/verify/github
│   │   ├── score.py             ← GET /api/score
│   │   └── badges.py            ← POST /api/badges/submit
│   └── services/
│       ├── __init__.py
│       ├── cv_parser.py         ← PDF text extraction + LLM skill extraction
│       ├── github_service.py    ← GitHub API calls + project matching
│       └── score_service.py     ← Score calculation engine
├── requirements.txt
├── .env.example
└── README.md
