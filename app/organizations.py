"""Organization membership never bypasses candidate consent."""
import json
import secrets
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.platform import current_user, db, decode, public_data
from app.domain import token_hash

router = APIRouter(prefix='/api/organizations', tags=['Organization workspaces'])


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Invite(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    role: Literal['administrator', 'reviewer'] = 'reviewer'


class Accept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class MemberRole(BaseModel):
    role: Literal['administrator', 'reviewer']


class Selection(BaseModel):
    revision: int = Field(ge=0)
    candidate_ids: list[int] = Field(max_length=3)


class Note(BaseModel):
    revision: int = Field(ge=0)
    text: str = Field(max_length=4000)


async def access(c, oid, uid, management=False):
    # Membership mutations and scoped writes serialize on the organization.
    if not await c.fetchval('SELECT id FROM organizations WHERE id=$1 FOR UPDATE', oid):
        raise HTTPException(404, 'Organization unavailable')
    role = await c.fetchval('SELECT role FROM organization_members WHERE organization_id=$1 AND user_id=$2', oid, uid)
    if not role:
        raise HTTPException(404, 'Organization unavailable')
    if management and role not in ('owner', 'administrator'):
        raise HTTPException(403, 'Organization management role required')
    return role


async def audit(c, oid, actor, action, target):
    await c.execute('INSERT INTO organization_audit(organization_id,actor_id,action,target) VALUES($1,$2,$3,$4)', oid, actor, action, str(target))


@router.get('')
async def listing(user=Depends(current_user)):
    async with db() as c:
        return [dict(r) for r in await c.fetch('SELECT o.id,o.name,m.role FROM organizations o JOIN organization_members m ON m.organization_id=o.id WHERE m.user_id=$1 ORDER BY o.name', user['id'])]


@router.post('', status_code=201)
async def create(body: Name, user=Depends(current_user)):
    if not body.name.strip():
        raise HTTPException(422, 'Enter an organization name')
    oid = secrets.token_urlsafe(18)
    async with db() as c:
        async with c.transaction():
            await c.execute('INSERT INTO organizations(id,name) VALUES($1,$2)', oid, body.name.strip())
            await c.execute("INSERT INTO organization_members(organization_id,user_id,role) VALUES($1,$2,'owner')", oid, user['id'])
            await audit(c, oid, user['id'], 'created', oid)
    return {'id': oid, 'name': body.name.strip(), 'role': 'owner'}


@router.post('/accept')
async def accept(body: Accept, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            invite = await c.fetchrow('SELECT * FROM organization_invites WHERE token_hash=$1', token_hash(body.token))
            if not invite:
                raise HTTPException(404, 'Invitation unavailable')
            await c.fetchval('SELECT id FROM organizations WHERE id=$1 FOR UPDATE', invite['organization_id'])
            invite = await c.fetchrow('SELECT * FROM organization_invites WHERE token_hash=$1 AND NOT revoked AND NOT accepted AND expires_at>NOW() FOR UPDATE', token_hash(body.token))
            if not invite or invite['email'] != user['email'].lower():
                raise HTTPException(404, 'Invitation unavailable for this account')
            await c.execute('INSERT INTO organization_members(organization_id,user_id,role) VALUES($1,$2,$3) ON CONFLICT DO NOTHING', invite['organization_id'], user['id'], invite['role'])
            await c.execute('UPDATE organization_invites SET accepted=TRUE WHERE id=$1', invite['id'])
            await audit(c, invite['organization_id'], user['id'], 'joined', user['id'])
    return {'organization_id': invite['organization_id']}


@router.get('/{oid}')
async def workspace(oid: str, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            role = await access(c, oid, user['id'])
            org = dict(await c.fetchrow('SELECT * FROM organizations WHERE id=$1', oid))
            members = [dict(r) for r in await c.fetch('SELECT m.user_id,m.role,u.display_name FROM organization_members m JOIN users u ON u.id=m.user_id WHERE m.organization_id=$1 ORDER BY m.user_id', oid)]
            saved = []
            unavailable = 0
            for r in await c.fetch('SELECT * FROM organization_profiles WHERE organization_id=$1 ORDER BY candidate_id', oid):
                if await c.fetchval('SELECT discoverable FROM users WHERE id=$1', r['candidate_id']):
                    saved.append({'candidate': await public_data(c, r['candidate_id']), 'note': r['note'], 'revision': r['revision']})
                else:
                    unavailable += 1
            selected = decode(org['candidate_ids'])
            allowed = {p['candidate']['profile']['id'] for p in saved}
            invites = [dict(r) for r in await c.fetch('SELECT id,email,role,expires_at,accepted,revoked FROM organization_invites WHERE organization_id=$1 ORDER BY expires_at DESC', oid)] if role in ('owner', 'administrator') else []
            history = [dict(r) for r in await c.fetch('SELECT actor_id,action,target,created_at FROM organization_audit WHERE organization_id=$1 ORDER BY id DESC LIMIT 100', oid)]
            return {**org, 'candidate_ids': [i for i in selected if i in allowed], 'role': role, 'members': members, 'profiles': saved, 'unavailable_count': unavailable, 'invitations': invites, 'history': history}


@router.post('/{oid}/invitations', status_code=201)
async def invite(oid: str, body: Invite, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            role = await access(c, oid, user['id'], True)
            if body.role == 'administrator' and role != 'owner':
                raise HTTPException(403, 'Only the owner can invite administrators')
            email = body.email.strip().lower()
            if email.count('@') != 1 or '.' not in email.split('@')[1]:
                raise HTTPException(422, 'Enter a valid email')
            token, iid = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
            await c.execute("INSERT INTO organization_invites(id,organization_id,email,role,token_hash,expires_at) VALUES($1,$2,$3,$4,$5,NOW()+INTERVAL '7 days')", iid, oid, email, body.role, token_hash(token))
            await audit(c, oid, user['id'], 'invited', iid)
    return {'id': iid, 'token': token, 'notice': 'Give this seven-day invitation token to the named recipient. No email has been sent.'}


@router.delete('/{oid}/invitations/{iid}')
async def revoke_invite(oid: str, iid: str, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await access(c, oid, user['id'], True)
            changed = await c.execute('UPDATE organization_invites SET revoked=TRUE WHERE id=$1 AND organization_id=$2', iid, oid)
            if changed == 'UPDATE 0':
                raise HTTPException(404, 'Invitation unavailable')
            await audit(c, oid, user['id'], 'invitation_revoked', iid)
    return {'revoked': True}


async def manage_member(c, oid, actor, target):
    role = await access(c, oid, actor, True)
    existing = await c.fetchval('SELECT role FROM organization_members WHERE organization_id=$1 AND user_id=$2', oid, target)
    if not existing:
        raise HTTPException(404, 'Member unavailable')
    if existing == 'owner' or (role != 'owner' and existing != 'reviewer'):
        raise HTTPException(403, 'This membership is protected')
    return role


@router.delete('/{oid}/members/{uid}')
async def remove(oid: str, uid: int, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await manage_member(c, oid, user['id'], uid)
            await c.execute('DELETE FROM organization_members WHERE organization_id=$1 AND user_id=$2', oid, uid)
            await c.execute('UPDATE organization_invites SET revoked=TRUE WHERE organization_id=$1 AND email=(SELECT LOWER(email) FROM users WHERE id=$2)', oid, uid)
            await audit(c, oid, user['id'], 'member_removed', uid)
    return {'removed': True}


@router.put('/{oid}/members/{uid}')
async def change_role(oid: str, uid: int, body: MemberRole, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            role = await manage_member(c, oid, user['id'], uid)
            if role != 'owner':
                raise HTTPException(403, 'Only the owner can change roles')
            await c.execute('UPDATE organization_members SET role=$3 WHERE organization_id=$1 AND user_id=$2', oid, uid, body.role)
            await audit(c, oid, user['id'], 'role_' + body.role, uid)
    return {'role': body.role}


@router.put('/{oid}/profiles/{cid}')
async def save_profile(oid: str, cid: int, body: Note, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await access(c, oid, user['id'])
            if not await c.fetchval('SELECT discoverable FROM users WHERE id=$1', cid):
                raise HTTPException(404, 'Candidate consent unavailable')
            old = await c.fetchrow('SELECT revision FROM organization_profiles WHERE organization_id=$1 AND candidate_id=$2', oid, cid)
            if (old['revision'] if old else 0) != body.revision:
                raise HTTPException(409, 'Review note changed. Preserve your draft and refresh.')
            await c.execute('INSERT INTO organization_profiles(organization_id,candidate_id,note,revision) VALUES($1,$2,$3,$4) ON CONFLICT(organization_id,candidate_id) DO UPDATE SET note=EXCLUDED.note,revision=EXCLUDED.revision', oid, cid, body.text, body.revision + 1)
            await audit(c, oid, user['id'], 'profile_note_saved', cid)
    return {'revision': body.revision + 1}


@router.put('/{oid}/comparison')
async def compare(oid: str, body: Selection, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await access(c, oid, user['id'])
            previous = await c.fetchval('SELECT revision FROM organizations WHERE id=$1', oid)
            if previous != body.revision:
                raise HTTPException(409, 'Comparison changed. Refresh before saving.')
            allowed = {r['candidate_id'] for r in await c.fetch('SELECT p.candidate_id FROM organization_profiles p JOIN users u ON u.id=p.candidate_id WHERE p.organization_id=$1 AND u.discoverable=TRUE', oid)}
            if len(set(body.candidate_ids)) != len(body.candidate_ids) or set(body.candidate_ids) - allowed:
                raise HTTPException(404, 'Choose distinct saved profiles with current consent')
            await c.execute('UPDATE organizations SET candidate_ids=$2::jsonb,revision=revision+1 WHERE id=$1', oid, json.dumps(body.candidate_ids))
            await audit(c, oid, user['id'], 'comparison_saved', oid)
    return {'revision': previous + 1}

@router.delete('/{oid}/profiles/{cid}')
async def remove_profile(oid: str, cid: int, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await access(c, oid, user['id'])
            changed = await c.execute('DELETE FROM organization_profiles WHERE organization_id=$1 AND candidate_id=$2', oid, cid)
            if changed == 'DELETE 0':
                raise HTTPException(404, 'Saved profile unavailable')
            selected = decode(await c.fetchval('SELECT candidate_ids FROM organizations WHERE id=$1', oid))
            await c.execute('UPDATE organizations SET candidate_ids=$2::jsonb,revision=revision+1 WHERE id=$1', oid, json.dumps([i for i in selected if i != cid]))
            await audit(c, oid, user['id'], 'profile_removed', cid)
    return {'removed': True}

class TransferOwner(BaseModel):
    new_owner_id: int = Field(gt=0)
    confirm_transfer: bool = False


@router.post('/{oid}/transfer-owner')
async def transfer_owner(oid: str, body: TransferOwner, user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            role = await access(c, oid, user['id'], True)
            if role != 'owner' or not body.confirm_transfer or body.new_owner_id == user['id']:
                raise HTTPException(403, 'Current owner must explicitly confirm transfer to another member')
            target = await c.fetchval("SELECT m.user_id FROM organization_members m JOIN users u ON u.id=m.user_id WHERE m.organization_id=$1 AND m.user_id=$2 AND u.account_state='active'", oid, body.new_owner_id)
            if not target:
                raise HTTPException(404, 'Choose an active existing member')
            await c.execute("UPDATE organization_members SET role='administrator' WHERE organization_id=$1 AND user_id=$2", oid, user['id'])
            await c.execute("UPDATE organization_members SET role='owner' WHERE organization_id=$1 AND user_id=$2", oid, body.new_owner_id)
            await audit(c, oid, user['id'], 'ownership_transferred', body.new_owner_id)
    return {'owner_id': body.new_owner_id}
