"""Owned source revision history and downstream references from stored snapshots."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from app.platform import current_user, db, decode
from app.eligibility import eligible
from app.verification import evidence_hash
from app.presentations import digest
from app.collection import claims

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


def publication_revision_ids(source, revisions):
    timestamp = source.get('source_revision')
    digest_value = source.get('snapshot_sha256')
    if not timestamp or not digest_value:
        return []
    return [
        revision['id'] for revision in revisions
        if revision['operation'] != 'DELETE'
        and same_revision(revision['payload'].get('updated_at'), timestamp)
        and publication_hash(revision['payload']) == digest_value
    ]


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
            for section in decode(r['data']).get('sections', []):
                for e in section.get('evidence', []):
                    if e.get('id') != eid:
                        continue
                    matches = publication_revision_ids(e, revisions)
                    if not e.get('source_revision') or not e.get('snapshot_sha256'):
                        binding = 'Historical publication includes this evidence; exact source revision unavailable'
                    elif len(matches) == 1:
                        binding = 'Exact stored source revision'
                    else:
                        binding = 'No unique matching timestamp and metadata hash; inspect historical coverage'
                    references.append({'kind': 'published_claim', 'id': r['share_id'], 'section': section.get('kind'), 'claim': section.get('text'), 'source_reference': e, 'source_revision_ids': matches, 'revoked': r['revoked'], 'binding': binding})
        for r in await c.fetch(
            """SELECT p.share_id,p.data,s.revoked,s.expires_at
               FROM recruiter_presentation_shares p JOIN shares s ON s.id=p.share_id
               WHERE s.user_id=$1 AND p.data->'evidence' @> jsonb_build_array(jsonb_build_object('id',$2::text))""",
            uid, eid,
        ):
            data = decode(r['data'])
            binding = data.get('_source_bindings', {}).get(eid)
            matches = [revision['id'] for revision in revisions
                       if binding and revision['id'] == binding['revision_id'] and revision['sha256'] == binding['sha256']]
            references.append({
                'kind': 'recruiter_presentation', 'id': r['share_id'], 'title': data['title'],
                'source_revision_ids': matches, 'revoked': r['revoked'], 'expires_at': r['expires_at'],
                'binding': 'Exact stored source revision' if matches else
                           'Historical presentation includes this evidence; exact source revision unavailable',
            })
        for r in await c.fetch('SELECT s.application_id,s.revision,s.data FROM application_snapshots s JOIN job_applications a ON a.id=s.application_id WHERE a.user_id=$1', uid):
            for e in decode(r['data'])['evidence']:
                if e['id'] == eid:
                    references.append({'kind': 'application_snapshot', 'id': r['application_id'], 'revision': r['revision'], 'source_snapshot': e, 'binding': 'Stored immutable submission source snapshot'})
        bindings = {row['claim_key']: dict(row) for row in await c.fetch(
            """SELECT claim_key,evidence_revision_id,claim_title,claim_kind,claim_origin,linked_at
               FROM claim_lineage_bindings WHERE user_id=$1 AND evidence_id=$2""", uid, eid,
        )}
        claim_links = await c.fetch(
            """SELECT claim_key FROM collection_links WHERE user_id=$1 AND evidence_id=$2
               UNION SELECT claim_key FROM claim_lineage_bindings WHERE user_id=$1 AND evidence_id=$2
               ORDER BY claim_key""", uid, eid,
        )
        if claim_links:
            matches = {row['claim_key']: [] for row in claim_links}
            for version in await c.fetch(
                'SELECT id,document_id,data,created_at FROM cv_versions WHERE user_id=$1 ORDER BY id DESC', uid,
            ):
                historical = claims(decode(version['data']), {})
                for key in matches.keys() & historical.keys():
                    claim = historical[key]
                    matches[key].append({
                        'cv_revision_id': version['id'], 'document_id': version['document_id'],
                        'created_at': version['created_at'], 'kind': claim['kind'], 'title': claim['title'],
                    })
            for key, versions in matches.items():
                attachment = bindings.get(key)
                references.append({
                    'kind': 'candidate_claim_link', 'id': key, 'matching_cv_revisions': versions,
                    'attachment': attachment,
                    'source_revision_ids': [attachment['evidence_revision_id']] if attachment else [],
                    'binding': 'Source revision captured at candidate attachment' if attachment else
                               'Historical candidate-selected source link with no recorded evidence revision',
                })
    return {'evidence_id': eid, 'currently_eligible': bool(current and eligible(dict(current))), 'withdrawn': current is None, 'revisions': revisions, 'downstream': references, 'scope': 'Owned provenance, not proof of claim truth. Migration baselines capture only available state. Earlier revisions are not invented. Downstream references remain historical when sources change or are withdrawn.'}
