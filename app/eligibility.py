from datetime import datetime,timezone

def eligible(evidence,now=None):
    if evidence.get('status')!='verified' or evidence.get('eligibility_state','current')!='current':return False
    due=evidence.get('review_due_at')
    if isinstance(due,str):
        try:due=datetime.fromisoformat(due.replace('Z','+00:00'))
        except ValueError:return False
    if due and due.tzinfo is None:return False
    return not due or due>(now or datetime.now(timezone.utc))
