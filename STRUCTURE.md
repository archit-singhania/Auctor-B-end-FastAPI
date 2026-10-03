# Active architecture

- `app/main.py`: lifecycle, explicit CORS, security headers and readiness.
- `app/platform.py`: owned API operations and durable parser-worker loop.
- `app/domain.py`: pure password, question-grading and score-v1 rules.
- `app/insights.py`: owned skills graph/roadmap, cache freshness, input snapshots and evidence-change comparisons.
- `app/services/cv_parser.py`: optional AI extraction with heuristic fallback.
- `app/db.py`, `schema.sql`: async PostgreSQL pooling and additive migrations.
- `app/manage.py`: private operator reviewer-role assignment.
- `tests/`: isolated multi-user integration journeys and pure rule tests.
- `docs/MANUAL_TESTS.md`: exact startup, synthetic inputs and expected ownership/grading/reviewer/share behavior.

The older router/service modules are preserved for migration reference and are not mounted in v2. Their demo-user contracts must not be reenabled.
