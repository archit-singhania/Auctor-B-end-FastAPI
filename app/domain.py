"""Pure security, grading and v1 score rules; no I/O or credentials."""
import hashlib
import hmac
import secrets

WEIGHTS = {"github": .25, "leetcode": .15, "badges": .30, "projects": .15, "experience": .15}

def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000)
    return f'pbkdf2_sha256$600000${salt}${digest.hex()}'

def check_password(password, encoded):
    try:
        _, iterations, salt, expected = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False

def score_from(evidence, passed_badges, projects, weights=None):
    weights = weights or WEIGHTS
    if any(v < 0 or v > 1 for v in weights.values()) or abs(sum(weights.values()) - 1) > .000001:
        raise ValueError('Score weights must be nonnegative and sum to 1')
    verified = [e for e in evidence if e['status'] == 'verified']
    bound = {e.get('detail', {}).get('project') for e in verified if e['kind'] == 'project'}
    solved = max([int(e.get('detail', {}).get('solved', 0)) for e in verified if e['kind'] == 'coding'] or [0])
    fractions = dict(github=float(any(e['kind']=='github' for e in verified)), leetcode=max(0,min(solved/300,1)), badges=min(len(set(passed_badges)) / 5,1), projects=sum(p.get('name') in bound for p in projects)/max(len(projects),1), experience=float(any(e['kind']=='experience' for e in verified)))
    return dict(total=round(sum(fractions[k]*weights[k] for k in WEIGHTS)*10,2), formula_version='v1', components={k:{'fraction':round(v,4),'weight':weights[k],'points':round(v*weights[k]*10,2)} for k,v in fractions.items()})

# Answers never leave the API until an owned attempt is submitted.
CATALOG = {
'jwt-auth': ('JWT & identity', 'JWT Authentication', [
('What does a signed JWT provide?', ['Integrity and authenticity of claims','Automatic claim encryption','A database session','Password recovery'],0),
('Which JWT field represents expiry?', ['sub','exp','iss','aud'],1),
('What must a JWT verifier validate?', ['Only the payload','Signature, allowed algorithm and claims','Only the header','Only token length'],1),
('How should a refresh token be protected?', ['Publish it in a URL','Store it in a controlled secure channel','Embed it in a public bundle','Send it to analytics'],1),
('What does revoking a refresh token prevent?', ['All old access tokens instantly','Future access-token renewal using it','All network requests','Key rotation'],1)]),
'docker': ('Containers & delivery', 'Docker', [
('Why use a multi-stage image build?', ['Separate build tooling from runtime','Increase image layers forever','Avoid dependency locking','Disable networks'],0),
('What does an image digest identify?', ['Mutable release name','Content-addressed image','Running process ID','Host IP'],1),
('How should production secrets enter a container?', ['Committed Dockerfile','Runtime secret injection','Public image layer','Git README'],1),
('What does a healthcheck describe?', ['Image copyright','Application readiness/liveness condition','Container age','CPU model'],1),
('Which user should ordinarily run an app?', ['Non-root with minimum permissions','Always root','Host admin','Any privileged user'],0)]),
'rest-api': ('API design', 'REST API', [
('Which operation is idempotent by HTTP semantics?', ['POST','PUT','CONNECT','PATCH always'],1),
('What is an appropriate validation-error response?', ['200 with an error','A 4xx response with safe field details','Always 500','No response'],1),
('How do you isolate tenant resources?', ['Trust client user IDs','Authorize authenticated ownership on server','Hide button','Use long names'],1),
('What helps clients retry a create operation safely?', ['Idempotency key','Changing IDs each retry','Unlimited retry','Disabling validation'],0),
('How should paginated APIs expose next pages?', ['Stable cursor or page contract','Random response order','Download all rows','Only through logs'],0)]),
'postgres': ('PostgreSQL & integrity', 'PostgreSQL', [
('What protects a debit and credit as one operation?', ['A transaction','Two async requests','A view','A string key'],0),
('What prevents duplicate unique values?', ['ORDER BY','Unique constraint','Frontend disabled button','A comment'],1),
('What helps evaluate an expensive query?', ['EXPLAIN ANALYZE','SELECT * again','More logging only','A new UI'],0),
('How do you prevent SQL injection?', ['Parameterized queries','Concatenating input','Trimming spaces','Hiding errors only'],0),
('What does SKIP LOCKED support?', ['Workers claiming different queued rows','Bypassing all permissions','Dropping locks','Faster backups'],0)]),
'redis': ('Caching & coordination', 'Redis', [
('What limits the lifetime of a cached item?', ['TTL','A key prefix','Value size','JSON schema'],0),
('What causes a cache stampede?', ['Many clients refilling one expired key','A valid TTL','Small keys','Atomic SET'],0),
('What should be authoritative for account balances?', ['Durable transactional store','Unbounded cache','Browser state','Logs only'],0),
('How do you acquire a basic expiring lock?', ['SET key token NX PX duration','GET then unguarded SET','APPEND always','KEYS *'],0),
('Why use a token when releasing a lock?', ['To avoid deleting another owner’s lock','To increase expiry','To reveal passwords','To compress values'],0)])}

def public_question(badge_id, number):
    q=CATALOG[badge_id][2][number]
    return dict(id=number,prompt=q[0],options=q[1])

def grade(badge_id, ids, answers):
    if set(answers) != {str(i) for i in ids}:
        raise ValueError('Answer every question exactly once')
    if any(type(v) is not int or not 0 <= v < 4 for v in answers.values()):
        raise ValueError('Invalid answer option')
    correct=sum(CATALOG[badge_id][2][i][2]==answers[str(i)] for i in ids)
    return correct, correct >= 3
