"""Personal recruiter exploration preferences and consent-aware comparisons."""
import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from app.platform import current_user, db, decode, public_data

router = APIRouter(prefix='/api/recruiter-exploration', tags=['Recruiter exploration'])


class Preferences(BaseModel):
    revision: int = Field(ge=0)
    density: Literal['comfortable', 'compact'] = 'comfortable'
    candidate_ids: list[int] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids) or any(i <= 0 for i in self.candidate_ids):
            raise ValueError('Choose up to three distinct candidates')
        return self


async def saved(c, uid):
    row = await c.fetchrow('SELECT revision,data FROM recruiter_exploration WHERE user_id=$1', uid)
    return {**decode(row['data']), 'revision': row['revision']} if row else {'revision': 0, 'density': 'comfortable', 'candidate_ids': []}


@router.get('')
async def read(user=Depends(current_user)):
    async with db() as c:
        value = await saved(c, user['id'])
        candidates = []
        for cid in value['candidate_ids']:
            if await c.fetchval('SELECT discoverable FROM users WHERE id=$1', cid):
                candidates.append(await public_data(c, cid))
        return {**value, 'candidates': candidates, 'unavailable_count': len(value['candidate_ids']) - len(candidates), 'scope': 'Personal comparison tray. Only currently discoverable profiles are available; organization membership does not grant access.'}


@router.put('')
async def write(body: Preferences, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', user['id'])
            previous = await saved(c, user['id'])
            if body.revision != previous['revision']:
                raise HTTPException(409, 'Comparison preferences changed. Reload before updating.')
            selected = {r['id'] for r in await c.fetch('SELECT id FROM users WHERE discoverable=TRUE AND id=ANY($1::int[])', body.candidate_ids)}
            if selected != set(body.candidate_ids):
                raise HTTPException(404, 'A selected profile is no longer discoverable. Remove unavailable profiles first.')
            value = body.model_dump(exclude={'revision'})
            await c.execute('INSERT INTO recruiter_exploration(user_id,revision,data) VALUES($1,$2,$3::jsonb) ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,data=EXCLUDED.data', user['id'], body.revision + 1, json.dumps(value))
            return {**value, 'revision': body.revision + 1}
