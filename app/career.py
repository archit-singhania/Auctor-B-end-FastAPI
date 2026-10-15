"""Owned, revision-bound career narratives. Narrative never verifies a claim."""
import json
from datetime import date as CalendarDate
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from app.platform import current_user, db, decode, event

router = APIRouter(prefix='/api/career-story', tags=['Career story'])

class Milestone(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    date: CalendarDate
    kind: Literal['project', 'experience', 'outcome'] = 'project'
    narrative: str = Field(default='', max_length=4000)
    annotation: str = Field(default='', max_length=500)
    visible: bool = False
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

class StoryPatch(BaseModel):
    revision: int = Field(ge=0)
    milestones: list[Milestone] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({m.id for m in self.milestones}) != len(self.milestones):
            raise ValueError('Milestone identifiers must be unique')
        if any(len(set(m.evidence_ids)) != len(m.evidence_ids) for m in self.milestones):
            raise ValueError('Evidence links must be unique')
        return self

async def read_story(c, uid):
    row = await c.fetchrow('SELECT revision,milestones FROM career_stories WHERE user_id=$1', uid)
    return {'revision':row['revision'], 'milestones':decode(row['milestones'])} if row else {'revision':0,'milestones':[]}

async def public_story(c, uid):
    story = await read_story(c, uid)
    allowed = {r['id']:dict(r) for r in await c.fetch("SELECT id,title,kind,status FROM evidence WHERE user_id=$1 AND status='verified'", uid)}
    return {'revision':story['revision'], 'milestones':[
        {**{k:m[k] for k in ('id','title','date','kind','narrative','annotation')},
         'narrative_status':'Candidate-written narrative; not independently verified',
         'evidence':[allowed[eid] for eid in m['evidence_ids'] if eid in allowed]}
        for m in story['milestones'] if m['visible']]}

@router.get('')
async def get_story(user=Depends(current_user)):
    async with db() as c: return await read_story(c, user['id'])

@router.put('')
async def save_story(body:StoryPatch,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            # Serialize first saves too, including the empty-story revision.
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE',user['id'])
            current = await read_story(c,user['id'])
            if current['revision'] != body.revision:
                raise HTTPException(409,'Story changed. Reload the saved version or preserve your draft before retrying.')
            ids = {eid for m in body.milestones for eid in m.evidence_ids}
            owned = {r['id'] for r in await c.fetch('SELECT id FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])',user['id'],list(ids))}
            if ids != owned: raise HTTPException(404,'Linked evidence is unavailable')
            milestones=[m.model_dump(mode='json') for m in body.milestones]
            await c.execute('INSERT INTO career_stories(user_id,revision,milestones) VALUES($1,$2,$3::jsonb) ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,milestones=EXCLUDED.milestones,updated_at=NOW()',user['id'],body.revision+1,json.dumps(milestones))
            await event(c,user['id'],'career_story','Career narrative updated; evidence verification unchanged')
            return {'revision':body.revision+1,'milestones':milestones}
