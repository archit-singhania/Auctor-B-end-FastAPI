"""Owned, deterministic evidence insight projections. No provider calls or new score weights."""
from collections import Counter
from datetime import datetime, timezone
import re

from app.domain import CATALOG


ALIASES = {
    'jwt-auth': {'jwt', 'jwtauthentication', 'jwtauth', 'jsonwebtoken'},
    'docker': {'docker', 'containers', 'containerization'},
    'rest-api': {'rest', 'restapi', 'restapis', 'restful', 'restfulapi'},
    'postgres': {'postgres', 'postgresql'},
    'redis': {'redis'},
}
PRACTICE = {
    'jwt-auth': 'Validate signature, allowed algorithm and claims; design safe token renewal.',
    'docker': 'Practice multi-stage builds, runtime secrets, health checks and least privilege.',
    'rest-api': 'Practice idempotency, tenant authorization, validation and stable pagination.',
    'postgres': 'Practice transactions, constraints, query plans and safe worker claims.',
    'redis': 'Practice TTLs, stampede control, authoritative stores and expiring locks.',
}


def skill_key(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def track_for(value):
    normalized = skill_key(value)
    return next((track for track, aliases in ALIASES.items() if normalized in aliases), None)


def signal_snapshot(cv, evidence, badges):
    return {
        'skills': sorted(set(cv.get('skills', []))),
        'projects': sorted(p.get('name', '') for p in cv.get('projects', [])),
        'badges': sorted(set(badges)),
        'evidence': sorted(({
            'id': e['id'], 'kind': e['kind'], 'title': e['title'], 'status': e['status'],
            'details': {k: e.get('detail', {}).get(k) for k in ('project', 'repository', 'solved', 'issuer', 'reference') if k in e.get('detail', {})},
        } for e in evidence), key=lambda e: e['id']),
    }


def score_comparisons(history):
    result = []
    ordered = sorted(history, key=lambda row: row['id'])
    for index, row in enumerate(ordered):
        before = ordered[index - 1] if index else None
        previous = before.get('signals', {}) if before else {}
        current = row.get('signals', {})
        old_evidence = {e['id']: e for e in previous.get('evidence', [])}
        new_evidence = {e['id']: e for e in current.get('evidence', [])}
        changes = []
        for eid, item in new_evidence.items():
            if eid not in old_evidence:
                changes.append(f"Added {item['kind']}: {item['title']} ({item['status']})")
            elif item != old_evidence[eid]:
                changes.append(f"Updated {item['title']}: {old_evidence[eid]['status']} → {item['status']}")
        changes += [f"Removed {e['kind']}: {e['title']}" for eid, e in old_evidence.items() if eid not in new_evidence]
        for group in ('skills', 'projects', 'badges'):
            added = set(current.get(group, [])) - set(previous.get(group, []))
            removed = set(previous.get(group, [])) - set(current.get(group, []))
            if added: changes.append(f"{group.title()} added: {', '.join(sorted(added))}")
            if removed: changes.append(f"{group.title()} removed: {', '.join(sorted(removed))}")
        deltas = {key: round(value['points'] - before['score']['components'].get(key, {}).get('points', 0), 2) for key, value in row['score']['components'].items()} if before else {}
        result.append({'history_id': row['id'], 'previous_id': before['id'] if before else None,
                       'total_delta': round(row['score']['total'] - before['score']['total'], 2) if before else None,
                       'component_deltas': deltas, 'changes': changes,
                       'baseline_known': bool(current) and before is not None and bool(previous)})
    return result


def build_insights(cv, evidence, attempts, github, history, now=None):
    now = now or datetime.now(timezone.utc)
    passed = {a['badge_id'] for a in attempts if a.get('passed')}
    nodes = {}
    def node(name):
        key = skill_key(name)
        if key not in nodes: nodes[key] = {'id': 'skill:' + key, 'name': name, 'sources': []}
        return nodes[key]
    for name in cv.get('skills', []):
        node(name)['sources'].append({'id': 'cv', 'label': 'Current CV declaration', 'kind': 'cv', 'status': 'unverified'})
    bound = {e.get('detail', {}).get('project'): e for e in evidence if e['kind'] == 'project' and e['status'] == 'verified'}
    for project in cv.get('projects', []):
        proof = bound.get(project['name'])
        for name in project.get('tech_stack', []):
            node(name)['sources'].append({'id': 'project:' + project['name'], 'label': project['name'], 'kind': 'project',
                                         'status': 'owned provenance' if proof else 'unverified',
                                         'evidence_id': proof['id'] if proof else None,
                                         'scope': 'Declared technology; repository ownership does not assess this skill'})
    for track in passed:
        matching = [n for n in nodes.values() if track_for(n['name']) == track]
        if not matching: matching = [node(CATALOG[track][1])]
        for n in matching:
            n['sources'].append({'id': 'badge:' + track, 'label': CATALOG[track][0], 'kind': 'assessment', 'status': 'assessed', 'badge_id': track,
                                 'scope': 'Passed this five-question server assessment; not comprehensive competency'})
    graph, roadmap = [], []
    for n in nodes.values():
        track = track_for(n['name'])
        n['status'] = 'assessed' if track in passed else ('owned provenance' if any(s['status'] == 'owned provenance' for s in n['sources']) else 'claimed')
        graph.append(n)
        roadmap.append({'skill': n['name'], 'track_id': track, 'status': n['status'],
                        'priority': 3 if track in passed else (1 if track else 2),
                        'reason': 'Assessment earned; deepen the practical evidence.' if track in passed else ('A CV/project claim has an available assessment gap.' if track else 'No implemented assessment exists for this skill.'),
                        'next_step': PRACTICE[track] if track else 'Build a demonstrable project and supply inspectable source evidence.',
                        'sources': len(n['sources'])})
    edges = [{'from': n['id'], 'to': s['id'], 'kind': s['kind'], 'status': s['status']} for n in graph for s in n['sources']]
    repos = github.get('repositories', [])
    age = None
    try:
        timestamp = datetime.fromisoformat(github['synced_at'].replace('Z', '+00:00'))
        age = max(0, int((now - timestamp).total_seconds()))
    except (KeyError, ValueError, TypeError):
        pass
    analytics = {'synced_at': github.get('synced_at'), 'age_seconds': age,
                 'freshness': 'unconnected' if age is None else ('current' if age < 86400 else ('aging' if age < 604800 else 'stale')),
                 'repository_count': len(repos), 'stars': sum(r.get('stars', 0) for r in repos),
                 'languages': dict(Counter(r.get('language') or 'Mixed' for r in repos)),
                 'event_types': dict(Counter(e['type'] for e in github.get('recent_activity', []))),
                 'scope': 'Cached public owned repositories and up to 100 recent public events; reconnect refreshes. Not complete contribution history.'}
    return {'skill_graph': {'nodes': graph, 'edges': edges},
            'roadmap': sorted(roadmap, key=lambda item: (item['priority'], item['skill'].lower())),
            'repository_analytics': analytics, 'score_comparisons': score_comparisons(history)}
