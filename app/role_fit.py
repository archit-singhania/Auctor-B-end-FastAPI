"""Versioned evidence coverage, deliberately independent of the developer score."""
import hashlib
import json
import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from typing import Literal
from app.platform import current_user, db, decode, cv_data, record
from app.insights import track_for, skill_key
from app.domain import CATALOG
from app.eligibility import eligible as evidence_eligible

FORMULA='evidenced-coverage-v1'
VOCABULARY='auctor-skills-v1'
router=APIRouter(prefix='/api/role-fit',tags=['Role fit'])

def key(skill): return track_for(skill) or skill_key(skill)

class Requirement(BaseModel):
    skill:str=Field(min_length=1,max_length=100)
    priority:Literal['required','preferred']='required'
    weight:float|None=Field(default=None,gt=0,le=10)

    @model_validator(mode='after')
    def defaults(self):
        self.skill=self.skill.strip()
        if not key(self.skill):raise ValueError('Enter a skill name')
        if self.weight is None:self.weight=2 if self.priority=='required' else 1
        return self

class RoleDocument(BaseModel):
    revision:int=Field(default=0,ge=0)
    title:str=Field(min_length=1,max_length=160)
    description:str=Field(default='',max_length=20000)
    reviewed:bool=False
    requirements:list[Requirement]=Field(default_factory=list,max_length=50)

    @model_validator(mode='after')
    def unique(self):
        if len({key(r.skill) for r in self.requirements})!=len(self.requirements):raise ValueError('Combine duplicate skill requirements')
        return self

def evaluate(document,cv,evidence,badges):
    eligible=[e for e in evidence if evidence_eligible(e)]
    declared={key(s) for s in cv.get('skills',[])}
    contributions=[]
    for r in document['requirements']:
        wanted=key(r['skill']);sources=[];partial=[]
        for badge in sorted(set(badges)):
            if key(badge)==wanted and badge in CATALOG:
                sources.append({'kind':'assessment','id':badge,'title':CATALOG[badge][0],'scope':'Passed the implemented five-question assessment; not comprehensive competency'})
        for e in eligible:
            skills=e.get('detail',{}).get('skills',[])
            if isinstance(skills,list) and any(isinstance(s,str) and key(s)==wanted for s in skills):
                sources.append({'kind':'evidence','id':e['id'],'title':e['title'],'scope':'Reviewer-eligible source explicitly lists this skill; not a hiring guarantee'})
            if e['kind']=='project':
                for p in cv.get('projects',[]):
                    if p.get('name')==e.get('detail',{}).get('project') and any(key(s)==wanted for s in p.get('tech_stack',[])):
                        partial.append({'kind':'evidence','id':e['id'],'title':e['title'],'scope':'Owned repository provenance; technology declaration remains unassessed'})
        status='supported' if sources else 'partial' if partial else 'declared-only' if wanted in declared else 'unknown'
        contributions.append({**r,'status':status,'earned_weight':r['weight'] if sources else 0,'sources':sources,'context':partial})
    total=sum(r['weight'] for r in contributions);earned=sum(r['earned_weight'] for r in contributions)
    snapshot={'document':document,'cv':cv,'evidence':evidence,'badges':sorted(set(badges))}
    return {'formula_version':FORMULA,'vocabulary_version':VOCABULARY,'input_sha256':hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest(),'inputs':snapshot,'contributions':contributions,'earned_weight':earned,'total_weight':total,'coverage_percent':round(100*earned/total,2) if total else 0,'explanation':'Coverage = supported requirement weights / all requirement weights. Partial provenance and declarations earn zero coverage. Missing evidence is unknown, not inability. This does not change the developer score.'}

async def document(c,uid):
    row=await c.fetchrow('SELECT revision,data FROM role_documents WHERE user_id=$1',uid)
    return {**decode(row['data']),'revision':row['revision']} if row else {'revision':0,'title':'Target role','description':'','reviewed':False,'requirements':[]}

@router.get('')
async def workspace(user=Depends(current_user)):
    async with db() as c:
        return {'document':await document(c,user['id']),'runs':[{'id':r['id'],'created_at':r['created_at'],'result':decode(r['result'])} for r in await c.fetch('SELECT id,created_at,result FROM role_fit_runs WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20',user['id'])]}

@router.put('')
async def save(body:RoleDocument,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE',user['id'])
            old=await document(c,user['id'])
            if old['revision']!=body.revision:raise HTTPException(409,'Role changed. Reload before replacing the saved requirements.')
            value=body.model_dump(mode='json');value['revision']+=1
            await c.execute('INSERT INTO role_documents(user_id,revision,data) VALUES($1,$2,$3::jsonb) ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,data=EXCLUDED.data',user['id'],value['revision'],json.dumps(value))
            return value

@router.post('/runs',status_code=201)
async def run(user=Depends(current_user)):
    async with db() as c:
        async with c.transaction(isolation='repeatable_read'):
            role=await document(c,user['id'])
            if not role['reviewed'] or not role['requirements']:raise HTTPException(400,'Review and save at least one requirement before matching')
            evidence=[record(r) for r in await c.fetch('SELECT id,kind,title,status,detail,review_due_at,eligibility_state FROM evidence WHERE user_id=$1 ORDER BY id',user['id'])]
            badges=[r['badge_id'] for r in await c.fetch('SELECT DISTINCT badge_id FROM attempts WHERE user_id=$1 AND passed=TRUE',user['id'])]
            result=evaluate(role,await cv_data(c,user['id']),evidence,badges);rid=secrets.token_urlsafe(18)
            await c.execute('INSERT INTO role_fit_runs(id,user_id,result) VALUES($1,$2,$3::jsonb)',rid,user['id'],json.dumps(result,default=str))
            return {'id':rid,'result':result}
