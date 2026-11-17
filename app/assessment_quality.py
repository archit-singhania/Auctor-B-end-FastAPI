"""Version-bound question delivery and reviewer-only quality reporting."""
import hashlib
import json
import math
import statistics
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from app.domain import CATALOG
from app.platform import db, reviewer, decode

router = APIRouter(prefix='/api/assessment-quality', tags=['Assessment quality'])


def baseline(badge):
    return {'version': 1, 'questions': [{'id': i, 'prompt': q[0], 'options': q[1], 'answer': q[2], 'retired': False} for i, q in enumerate(CATALOG[badge][2])]}


async def catalog(c, badge):
    if badge not in CATALOG:
        raise HTTPException(404, 'Assessment unavailable')
    row = await c.fetchrow('SELECT version,data FROM assessment_versions WHERE badge_id=$1 ORDER BY version DESC LIMIT 1', badge)
    return {'version': row['version'], 'questions': decode(row['data'])} if row else baseline(badge)


def public_questions(snapshot, ids):
    by_id = {q['id']: q for q in snapshot['questions']}
    return [{k: by_id[i][k] for k in ('id', 'prompt', 'options')} for i in ids]


def grade_snapshot(snapshot, ids, answers):
    if set(answers) != {str(i) for i in ids} or any(type(v) is not int or not 0 <= v < 4 for v in answers.values()):
        raise ValueError('Answer every question exactly once with a valid option')
    by_id = {q['id']: q for q in snapshot['questions']}
    correct = sum(by_id[i]['answer'] == answers[str(i)] for i in ids)
    return correct, correct >= 3


class Question(BaseModel):
    id: int = Field(ge=0, le=4)
    prompt: str = Field(min_length=3, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    answer: int = Field(ge=0, le=3)
    retired: bool = False

    @model_validator(mode='after')
    def usable(self):
        if any(not v.strip() or len(v) > 500 for v in self.options):
            raise ValueError('Each option needs 1–500 characters')
        return self


class Publication(BaseModel):
    version: int = Field(ge=1)
    questions: list[Question] = Field(min_length=5, max_length=5)
    reason: str = Field(min_length=10, max_length=2000)
    confirm_publication: bool = False

    @model_validator(mode='after')
    def ids(self):
        if {q.id for q in self.questions} != set(range(5)):
            raise ValueError('Preserve the five unique question identifiers')
        return self


@router.get('/{badge}/catalog')
async def read_catalog(badge: str, user=Depends(reviewer)):
    async with db() as c:
        value = await catalog(c, badge)
        history = [dict(r) for r in await c.fetch('SELECT version,reviewer_id,reason,created_at FROM assessment_versions WHERE badge_id=$1 ORDER BY version', badge)]
    return {**value, 'history': history}


@router.post('/{badge}/versions', status_code=201)
async def publish(badge: str, body: Publication, user=Depends(reviewer)):
    if not body.confirm_publication:
        raise HTTPException(400, 'Human review and explicit publication confirmation required')
    async with db() as c:
        async with c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(73722902)')
            current = await catalog(c, badge)
            if current['version'] != body.version:
                raise HTTPException(409, 'Question version changed. Review the current version first.')
            questions = [q.model_dump() for q in sorted(body.questions, key=lambda q: q.id)]
            await c.execute('INSERT INTO assessment_versions(badge_id,version,data,reviewer_id,reason) VALUES($1,$2,$3::jsonb,$4,$5)', badge, body.version + 1, json.dumps(questions), user['id'], body.reason)
    return {'version': body.version + 1, 'available': not any(q['retired'] for q in questions)}


def correlation(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    numerator = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    denominator = math.sqrt(sum((x-mx)**2 for x in xs)*sum((y-my)**2 for y in ys))
    return numerator / denominator if denominator else None


def summarize(rows, version, now):
    compatible = [r for r in rows if r['snapshot'] and r['snapshot']['version'] == version]
    valid = []
    for r in compatible:
        if r['submitted_at'] is None or r['submitted_at'] > r['expires_at'] or r['submitted_at'] < r['started_at']:
            continue
        try:
            correct, _ = grade_snapshot(r['snapshot'], r['ids'], r['answers'])
            if correct == r['correct_count']:
                valid.append(r)
        except (ValueError, TypeError, KeyError):
            continue
    closed = [r for r in compatible if r['submitted_at'] is not None or r['expires_at'] <= now]
    result = {'formula_version': 'quality-v1', 'question_version': version, 'eligible_completed': len(valid), 'closed_attempts': len(closed), 'suppressed': len(valid) < 20, 'scope': 'Attempt cohorts may include repeat candidates. Timing is whole-attempt duration; per-question timing is unavailable. Integrity flags do not change scores or cohort eligibility automatically.'}
    if result['suppressed']:
        return result
    durations = sorted((r['submitted_at']-r['started_at']).total_seconds() for r in valid)
    items = []
    for qid in range(5):
        responses = [next(q for q in r['snapshot']['questions'] if q['id']==qid)['answer'] == r['answers'][str(qid)] for r in valid]
        rest = [r['correct_count']-int(correct) for r,correct in zip(valid,responses)]
        items.append({'question_id': qid, 'completion_rate': len(valid)/len(closed) if closed else None, 'observed_correct_fraction': sum(responses)/len(valid), 'item_rest_correlation': correlation(responses,rest)})
    return {**result, 'unstable': len(valid) < 100, 'duration_seconds': {'min': durations[0], 'median': statistics.median(durations), 'p90': durations[math.ceil(.9*len(durations))-1], 'max': durations[-1]}, 'items': items}


@router.get('/{badge}/report')
async def report(badge: str, version: int | None = None, user=Depends(reviewer)):
    from datetime import datetime, timezone
    async with db() as c:
        current = await catalog(c, badge)
        chosen = version or current['version']
        if chosen < 1 or chosen > current['version']:
            raise HTTPException(404, 'Version unavailable')
        rows = await c.fetch('SELECT id,question_snapshot,question_ids,answers,correct_count,started_at,submitted_at,expires_at FROM attempts WHERE badge_id=$1 ORDER BY id LIMIT 10001', badge)
        if len(rows) > 10000:
            raise HTTPException(409, 'Cohort exceeds the synchronous report limit; an export worker is required')
        values = [dict(r, snapshot=decode(r['question_snapshot']), ids=decode(r['question_ids']), answers=decode(r['answers'])) for r in rows]
        as_of = datetime.now(timezone.utc)
        result = summarize(values, chosen, as_of)
        result['as_of'] = as_of.isoformat()
        result['legacy_attempts_without_snapshot'] = sum(r['snapshot'] is None for r in values)
        result['input_sha256'] = hashlib.sha256(json.dumps([dict(r) for r in rows], sort_keys=True, default=str).encode()).hexdigest()
        result['integrity_signals_separate'] = await c.fetchval('SELECT COUNT(*) FROM assessment_integrity_signals s JOIN attempts a ON a.id=s.attempt_id WHERE a.badge_id=$1', badge)
    return result
