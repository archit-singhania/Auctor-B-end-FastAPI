# Active architecture

- `app/main.py`: lifecycle, explicit CORS, security headers and readiness.
- `app/platform.py`: owned API operations and durable parser-worker loop.
- `app/domain.py`: pure password, question-grading and score-v1 rules.
- `app/services/cv_parser.py`: optional AI extraction with heuristic fallback.
- `app/db.py`, `schema.sql`: async PostgreSQL pooling and additive migrations.
- `app/manage.py`: private operator reviewer-role assignment.
- `tests/`: isolated multi-user integration journeys and pure rule tests.

The older router/service modules are preserved for migration reference and are not mounted in v2. Their demo-user contracts must not be reenabled.
