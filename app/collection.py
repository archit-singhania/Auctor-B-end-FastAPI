"""Private evidence collection suggestions derived from saved claims and review state."""
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.career import read_story
from app.eligibility import eligible
from app.platform import current_user, cv_data, db, event

router = APIRouter(prefix='/api/evidence-collection', tags=['Evidence collection'])
GUIDES = {
    'project': ('Link the repository or project deliverable.', 'Identify your own contribution and the relevant revision.', 'A repository can document work, but shared authorship needs separate evidence.'),
    'experience': ('Choose a dated employer or client source.', 'Describe the role, scope and period covered.', 'An uploaded letter remains a candidate submission until reviewed.'),
    'skill': ('Select a concrete work sample or assessed result.', 'Identify where the skill was demonstrated.', 'A certificate records its stated scope. It does not establish every use of the skill.'),
    'outcome': ('Identify the measurement and observation period.', 'Attach the source and distinguish your contribution.', 'An outcome alone does not establish causation.'),
}
KINDS = {'project':{'project'}, 'experience':{'experience'}, 'skill':{'project','coding','certificate'}, 'outcome':{'project','experience'}}

def claims(cv, story):
    result = {}
    def add(kind,title,origin,value,links=()):
        digest=hashlib.sha256(json.dumps([origin,kind,value],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        result[digest]={'id':digest,'kind':kind,'title':title,'origin':origin,'linked_ids':list(links)}
    for skill in cv.get('skills',[]):
        if isinstance(skill,str) and skill.strip():add('skill',skill,'CV',skill)
    for p in cv.get('projects',[]):
        if isinstance(p,dict) and p.get('name'):add('project',p['name'],'CV',p)
    for exp in cv.get('experience',[]):
        if isinstance(exp,dict):add('experience',' · '.join(str(exp[k]) for k in ('role','company') if exp.get(k)) or 'Experience','CV',exp)
    for m in story.get('milestones',[]):
        add(m['kind'],m['title'],'Career story',{k:m.get(k) for k in ('id','title','kind','date','narrative')},m.get('evidence_ids',[]))
    return result

def describe(claim,evidence,attached):
    ids=set(claim['linked_ids'])|set(attached)
    linked=[e for e in evidence if e['id'] in ids]
    suitable=[e for e in linked if e['kind'] in KINDS[claim['kind']]]
    if not linked:status='missing source'
    elif not suitable:status='weak link'
    elif any(eligible(e) for e in suitable):status='current reviewed source linked'
    elif all(e.get('eligibility_state')=='revoked' for e in suitable):status='linked sources revoked'
    elif any(e.get('status')=='verified' and not eligible(e) for e in suitable):status='source needs re-review'
    else:status='no eligible reviewed source'
    return {**{k:v for k,v in claim.items() if k!='linked_ids'},'status':status,'checklist':GUIDES[claim['kind']],
            'linked':linked,'scope':'Candidate-selected links organize sources. Reviewer decisions apply to the evidence and its stated scope.'}

async def owned_claims(c,uid):
    return claims(await cv_data(c,uid),await read_story(c,uid))

class Attachment(BaseModel):
    evidence_id: str = Field(min_length=1,max_length=120)

@router.get('')
async def checklist(user=Depends(current_user)):
    async with db() as c:
        uid=user['id']; saved=await owned_claims(c,uid)
        evidence=[dict(row) for row in await c.fetch('SELECT id,title,kind,status,eligibility_state,review_due_at FROM evidence WHERE user_id=$1 ORDER BY title,id',uid)]
        links={}
        for row in await c.fetch('SELECT claim_key,evidence_id FROM collection_links WHERE user_id=$1',uid):
            links.setdefault(row['claim_key'],[]).append(row['evidence_id'])
        return {'claims':[describe(v,evidence,links.get(k,[])) for k,v in saved.items()],'available_evidence':evidence}

@router.post('/{claim_id}/links')
async def attach(claim_id:str,body:Attachment,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            uid=user['id']
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE',uid)
            if claim_id not in await owned_claims(c,uid):raise HTTPException(409,'Claim changed. Refresh the collection checklist.')
            if not await c.fetchval('SELECT id FROM evidence WHERE id=$1 AND user_id=$2',body.evidence_id,uid):raise HTTPException(404,'Evidence unavailable.')
            added=await c.fetchval('INSERT INTO collection_links(user_id,claim_key,evidence_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING evidence_id',uid,claim_id,body.evidence_id)
            if added:await event(c,uid,'collection_link','Candidate linked an existing source to a claim.')
            return {'claim_id':claim_id,'evidence_id':body.evidence_id}
