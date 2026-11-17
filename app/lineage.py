"""Owned source revision history and downstream references from stored snapshots."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from app.platform import current_user, db, decode
from app.eligibility import eligible
from app.verification import evidence_hash
from app.presentations import digest

router = APIRouter(prefix='/api/lineage', tags=['Evidence lineage'])


def same_revision(left, right):
    try:
        return datetime.fromisoformat(str(left).replace('Z', '+00:00')) == datetime.fromisoformat(str(right).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return False


def receipt_hash(payload):
    normalized = dict(payload)
    for key in ('updated_at', 'review_due_at'):
        if normalized.get(key):
            normalized[key] = datetime.fromisoformat(str(normalized[key]).replace('Z', '+00:00')).astimezone(timezone.utc)
    return evidence_hash(normalized)


def publication_hash(payload):
    snapshot = {key: payload.get(key) for key in ('id', 'title', 'kind', 'status', 'eligibility_state', 'review_due_at', 'updated_at')}
    for key in ('updated_at', 'review_due_at'):
        if snapshot.get(key):
            snapshot[key] = datetime.fromisoformat(str(snapshot[key]).replace('Z', '+00:00')).astimezone(timezone.utc)
    return digest(snapshot)


@router.get('')
async def sources(user=Depends(current_user)):
    async with db() as c:
        return [dict(r) for r in await c.fetch("SELECT DISTINCT ON(evidence_id) evidence_id,payload->>'title' AS title,operation,captured_at FROM evidence_lineage_snapshots WHERE user_id=$1 ORDER BY evidence_id,id DESC", user['id'])]


@router.get('/{eid}')
async def trace(eid: str, user=Depends(current_user)):
    uid = user['id']
    async with db() as c:
        rows = await c.fetch('SELECT * FROM evidence_lineage_snapshots WHERE user_id=$1 AND evidence_id=$2 ORDER BY id', uid, eid)
        if not rows:
            raise HTTPException(404, 'Source lineage unavailable')
        revisions = [dict(r, payload=decode(r['payload'])) for r in rows]
        current = await c.fetchrow('SELECT * FROM evidence WHERE user_id=$1 AND id=$2', uid, eid)
        references = []
        for r in await c.fetch('SELECT id,status,note,source,created_at FROM review_decisions WHERE user_id=$1 AND evidence_id=$2 ORDER BY id', uid, eid):
            references.append({'kind': 'review_decision', 'id': str(r['id']), 'snapshot': decode(r['source']), 'status': r['status'], 'created_at': r['created_at'], 'binding': 'Stored reviewer input snapshot; historical source fields may be incomplete'})
        for r in await c.fetch('SELECT id,payload,revoked FROM verification_receipts WHERE user_id=$1 AND evidence_id=$2', uid, eid):
            payload = decode(r['payload'])
            matches = [s['id'] for s in revisions if receipt_hash(s['payload']) == payload['evidence_snapshot_hash']]
            references.append({'kind': 'receipt', 'id': r['id'], 'snapshot_hash': payload['evidence_snapshot_hash'], 'source_revision_ids': matches, 'revoked': r['revoked'], 'binding': 'Receipt hash exact match' if matches else 'Historical signed hash; matching source payload unavailable'})
        for r in await c.fetch('SELECT p.share_id,p.data,s.revoked FROM case_study_publications p JOIN shares s ON s.id=p.share_id WHERE s.user_id=$1', uid):
            for section in decode(r['data'])['sections']:
                for e in section['evidence']:
                    if e['id'] != eid:
                        continue
                    matches = [s['id'] for s in revisions if same_revision(s['payload'].get('updated_at'), e['source_revision']) and s['operation'] != 'DELETE' and publication_hash(s['payload']) == e['snapshot_sha256']]
                    references.append({'kind': 'published_claim', 'id': r['share_id'], 'section': section['kind'], 'claim': section['text'], 'source_reference': e, 'source_revision_ids': matches, 'revoked': r['revoked'], 'binding': 'Exact stored source revision' if len(matches) == 1 else 'No unique matching timestamp and metadata hash; inspect historical coverage'})
        for r in await c.fetch('SELECT s.application_id,s.revision,s.data FROM application_snapshots s JOIN job_applications a ON a.id=s.application_id WHERE a.user_id=$1', uid):
            for e in decode(r['data'])['evidence']:
                if e['id'] == eid:
                    references.append({'kind': 'application_snapshot', 'id': r['application_id'], 'revision': r['revision'], 'source_snapshot': e, 'binding': 'Stored immutable submission source snapshot'})
        for r in await c.fetch('SELECT claim_key FROM collection_links WHERE user_id=$1 AND evidence_id=$2', uid, eid):
            references.append({'kind': 'candidate_claim_link', 'id': r['claim_key'], 'binding': 'Candidate-selected live source link; no historical revision binding'})
    return {'evidence_id': eid, 'currently_eligible': bool(current and eligible(dict(current))), 'withdrawn': current is None, 'revisions': revisions, 'downstream': references, 'scope': 'Owned provenance, not proof of claim truth. Migration baselines capture only available state. Earlier revisions are not invented. Downstream references remain historical when sources change or are withdrawn.'}
