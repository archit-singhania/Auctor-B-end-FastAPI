from datetime import datetime, timedelta, timezone
from app.assessment_quality import baseline, summarize


def test_small_cohorts_suppressed_and_item_rest_statistics_reproduce():
    now = datetime.now(timezone.utc)
    snapshot = baseline('docker')
    rows = []
    for i in range(20):
        correct = i % 2 == 0
        answers = {str(q['id']): q['answer'] if correct else (q['answer'] + 1) % 4 for q in snapshot['questions']}
        rows.append({'snapshot': snapshot, 'ids': list(range(5)), 'answers': answers, 'correct_count': 5 if correct else 0, 'started_at': now-timedelta(seconds=20+i), 'submitted_at': now, 'expires_at': now+timedelta(seconds=30)})
    suppressed = summarize(rows[:19], 1, now)
    assert suppressed['suppressed'] and 'items' not in suppressed and 'duration_seconds' not in suppressed
    report = summarize(rows, 1, now)
    assert not report['suppressed'] and report['unstable']
    assert report['duration_seconds']['median'] == 29.5
    assert report['duration_seconds']['p90'] == 37
    for item in report['items']:
        assert item['observed_correct_fraction'] == .5
        assert item['completion_rate'] == 1
        assert item['item_rest_correlation'] == 1
    rows[0]['correct_count'] = 4
    assert summarize(rows, 1, now)['suppressed']
