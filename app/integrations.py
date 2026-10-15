"""Owner-consented profile credentials and pinned-address signed webhook delivery."""
import asyncio
from contextlib import suppress
from datetime import datetime,timezone,timedelta
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
from urllib.parse import urlsplit,quote
from cryptography.fernet import Fernet
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel,Field
from app.platform import current_user,db,decode,rate_limit
from app.config import settings
from app.verification_keys import directory,exclusive,canonical

router=APIRouter(prefix='/api/integrations',tags=['Consented recruiter integrations'])
SCOPES={'profile:read','verification:read'}

def cipher():
    path=directory()/'webhooks.fernet'
    if path.is_symlink():raise RuntimeError('Webhook key cannot be a symlink')
    if not path.exists():
        try:exclusive(path,Fernet.generate_key())
        except FileExistsError:pass
    return Fernet(path.read_bytes())

async def destination(value):
    if any(ord(c)<32 or ord(c)==127 for c in value):raise HTTPException(422,'Invalid webhook URL')
    try:
        url=urlsplit(value);port=url.port or (443 if url.scheme=='https' else 80)
        host=(url.hostname or '').encode('idna').decode('ascii')
    except (ValueError,UnicodeError):raise HTTPException(422,'Invalid webhook address')
    if not host or url.username or url.password or url.fragment or url.scheme not in ('https','http'):raise HTTPException(422,'Use an HTTPS destination without credentials or fragments')
    development=settings.webhook_development_mode and settings.app_env!='production'
    loopback_name=host in ('localhost','127.0.0.1','::1')
    if port!=443 and not(development and loopback_name):raise HTTPException(422,'Public webhooks must use HTTPS port 443')
    if url.scheme!='https' and not(development and loopback_name):raise HTTPException(422,'Webhook delivery requires HTTPS')
    try:addresses=sorted({info[4][0] for info in await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo,host,port,0,socket.SOCK_STREAM),5)})
    except (OSError,TimeoutError):raise HTTPException(422,'Webhook host could not be resolved')
    if not addresses:raise HTTPException(422,'Webhook host could not be resolved')
    for address in addresses:
        ip=ipaddress.ip_address(address)
        safe=ip.is_global and not ip.is_multicast and not ip.is_reserved and not (ip.version==6 and (ip.sixtofour or ip.teredo))
        if not safe and not(development and loopback_name and ip.is_loopback):raise HTTPException(422,'Webhook destinations cannot resolve to private or local addresses')
    return url,host,port,addresses[0]

async def pinned_post(value,payload,headers,consent_check=None):
    url,host,port,address=await destination(value)
    if consent_check is not None and not await consent_check():raise PermissionError('Consent withdrawn before connection')
    # DNS is checked again per delivery; connect to the checked IP with original TLS SNI.
    async def send():
        reader,writer=await asyncio.open_connection(address,port,ssl=ssl.create_default_context() if url.scheme=='https' else None,server_hostname=host if url.scheme=='https' else None,limit=16384)
        try:
            path=quote(url.path or '/',safe="/%:@!$&'()*+,;=-._~")+('?' +quote(url.query,safe="%=&?/:@!$'()*+,;=-._~") if url.query else '')
            authority=('['+host+']' if ':' in host else host)+(f':{port}' if port not in (80,443) else '')
            fields={'Host':authority,'Content-Type':'application/json','Content-Length':str(len(payload)),'Connection':'close',**headers}
            request=('POST '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in fields.items())+'\r\n').encode('ascii')+payload
            writer.write(request);await writer.drain();line=await reader.readline();parts=line.split()
            if len(parts)<2 or not parts[0].startswith(b'HTTP/1.') or not parts[1].isdigit():raise ValueError('Invalid webhook response')
            status=int(parts[1])
            if status<200 or status>=300:raise ValueError('Webhook returned a non-success status; redirects are not followed')
            return status
        finally:
            writer.close()
            with suppress(Exception):await asyncio.wait_for(writer.wait_closed(),1)
    return await asyncio.wait_for(send(),15)

class Consent(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    scopes:list[str]=Field(min_length=1,max_length=2)
    webhook_url:str=Field(default='',max_length=2048)
    expires_days:int=Field(default=30,ge=1,le=365)
    confirm_consent:bool=False

@router.post('',status_code=201)
async def create(body:Consent,user=Depends(current_user)):
    if not body.confirm_consent or set(body.scopes)-SCOPES:raise HTTPException(400,'Confirm the permitted profile/verification read scopes')
    if body.webhook_url:
        if 'verification:read' not in body.scopes:raise HTTPException(400,'Verification consent is required for update webhooks')
        await destination(body.webhook_url)
    iid=secrets.token_urlsafe(18);token='ats_'+secrets.token_urlsafe(32);secret=secrets.token_urlsafe(32)
    expires=datetime.now(timezone.utc)+timedelta(days=body.expires_days)
    async with db() as c:
        await c.execute('INSERT INTO recruiter_integrations(id,user_id,name,token_hash,scopes,webhook_url,secret_cipher,expires_at) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8)',iid,user['id'],body.name,hashlib.sha256(token.encode()).hexdigest(),json.dumps(sorted(set(body.scopes))),body.webhook_url,cipher().encrypt(secret.encode()).decode(),expires)
    return {'id':iid,'credential':token,'webhook_secret':secret if body.webhook_url else None,'expires_at':expires,'notice':'Store these now. Secrets are shown only once; revoke and recreate if lost.'}

@router.get('')
async def list_integrations(user=Depends(current_user)):
    async with db() as c:
        rows=await c.fetch('SELECT id,name,scopes,webhook_url,revoked,expires_at,created_at FROM recruiter_integrations WHERE user_id=$1 ORDER BY created_at DESC',user['id'])
        return [{**dict(r),'scopes':decode(r['scopes'])} for r in rows]

@router.delete('/{iid}')
async def revoke(iid:str,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            result=await c.execute('UPDATE recruiter_integrations SET revoked=TRUE WHERE id=$1 AND user_id=$2',iid,user['id'])
            if result=='UPDATE 0':raise HTTPException(404,'Integration unavailable')
            await c.execute("UPDATE integration_outbox SET status='revoked' WHERE integration_id=$1 AND status IN ('queued','dispatching')",iid)
    return {'ok':True}

@router.get('/{iid}/deliveries')
async def deliveries(iid:str,user=Depends(current_user)):
    async with db() as c:
        if not await c.fetchval('SELECT id FROM recruiter_integrations WHERE id=$1 AND user_id=$2',iid,user['id']):raise HTTPException(404,'Integration unavailable')
        return [dict(r) for r in await c.fetch('SELECT event_id,status,attempts,last_error,created_at,delivered_at FROM integration_outbox WHERE integration_id=$1 ORDER BY created_at DESC LIMIT 100',iid)]

async def authorized(request,scope):
    rate_limit(request,'recruiter-read',60)
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer ats_'):raise HTTPException(401,'Scoped recruiter credential required')
    hashed=hashlib.sha256(value[7:].encode()).hexdigest()
    async with db() as c:found=await c.fetchrow('SELECT user_id,scopes FROM recruiter_integrations WHERE token_hash=$1 AND revoked=FALSE AND expires_at>NOW()',hashed)
    if not found:raise HTTPException(401,'Credential expired or revoked')
    if scope not in decode(found['scopes']):raise HTTPException(403,'Credential lacks this read scope')
    return found['user_id']

@router.get('/recruiter/profile')
async def recruiter_profile(request:Request):
    uid=await authorized(request,'profile:read')
    async with db() as c:
        user=await c.fetchrow('SELECT display_name,handle FROM users WHERE id=$1',uid)
        evidence=await c.fetch("SELECT id,title,kind,status FROM evidence WHERE user_id=$1 AND status='verified' AND eligibility_state='current' AND (review_due_at IS NULL OR review_due_at>NOW())",uid)
    return {'profile':dict(user),'evidence':[dict(e) for e in evidence],'scope':'Consented profile fields only. Contact details and private source files are excluded.'}

@router.get('/recruiter/verification')
async def recruiter_verification(request:Request):
    uid=await authorized(request,'verification:read')
    async with db() as c:rows=await c.fetch('SELECT id,kind,status,eligibility_state,review_due_at FROM evidence WHERE user_id=$1',uid)
    return {'evidence':[dict(r) for r in rows],'scope':'Verification status metadata only; no private proof files or contact fields'}

async def enqueue(c,uid,kind):
    event_type='verification.updated' if kind in ('verification','review','github','challenge') else 'profile.updated'
    if kind not in ('verification','review','github','challenge','cv','profile','career_story'):return
    rows=await c.fetch("SELECT id FROM recruiter_integrations WHERE user_id=$1 AND revoked=FALSE AND expires_at>NOW() AND webhook_url<>'' AND scopes ? 'verification:read'",uid)
    for subscription in rows:
        eid=secrets.token_urlsafe(24);payload={'version':1,'event_id':eid,'type':event_type,'occurred_at':datetime.now(timezone.utc).isoformat(),'notice':'Use the scoped API for permitted current fields. This event contains no contact or private source data.'}
        await c.execute('INSERT INTO integration_outbox(event_id,integration_id,payload) VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING',eid,subscription['id'],json.dumps(payload))

async def deliver_one():
    async with db() as c:
        async with c.transaction():
            await c.execute("UPDATE integration_outbox SET status=CASE WHEN attempts>=5 THEN 'dead' ELSE 'queued' END WHERE status='dispatching' AND updated_at<NOW()-INTERVAL '2 minutes'")
            row=await c.fetchrow("SELECT * FROM integration_outbox WHERE status='queued' AND attempts<5 AND next_attempt_at<=NOW() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1")
            if not row:return False
            await c.execute("UPDATE integration_outbox SET status='dispatching',attempts=attempts+1,updated_at=NOW() WHERE event_id=$1",row['event_id'])
        subscription=await c.fetchrow("SELECT * FROM recruiter_integrations WHERE id=$1 AND revoked=FALSE AND expires_at>NOW() AND scopes ? 'verification:read'",row['integration_id'])
        if not subscription:
            await c.execute("UPDATE integration_outbox SET status='revoked',updated_at=NOW() WHERE event_id=$1",row['event_id']);return True
    try:
        payload=canonical(decode(row['payload']));timestamp=str(int(datetime.now(timezone.utc).timestamp()));secret=cipher().decrypt(subscription['secret_cipher'].encode())
        signature=hmac.new(secret,timestamp.encode()+b'.'+payload,hashlib.sha256).hexdigest()
        async def consent_check():
            async with db() as current:return bool(await current.fetchval("SELECT id FROM recruiter_integrations WHERE id=$1 AND revoked=FALSE AND expires_at>NOW() AND scopes ? 'verification:read'",subscription['id']))
        await pinned_post(subscription['webhook_url'],payload,{'X-Auctor-Event-Id':row['event_id'],'X-Auctor-Timestamp':timestamp,'X-Auctor-Signature':'v1='+signature},consent_check)
        async with db() as c:await c.execute("UPDATE integration_outbox SET status='delivered',delivered_at=NOW(),updated_at=NOW(),last_error=NULL WHERE event_id=$1 AND status='dispatching'",row['event_id'])
    except Exception:
        attempts=row['attempts']+1;deadline=datetime.now(timezone.utc)+timedelta(seconds=min(3600,2**attempts))
        async with db() as c:await c.execute("UPDATE integration_outbox SET status=$2,next_attempt_at=$3,updated_at=NOW(),last_error='Delivery failed; destination, consent, key or response unavailable' WHERE event_id=$1 AND status='dispatching'",row['event_id'],'dead' if attempts>=5 else 'queued',deadline)
    return True

async def outbox_worker():
    while True:
        try:worked=await deliver_one()
        except asyncio.CancelledError:raise
        except Exception:worked=False
        await asyncio.sleep(.2 if worked else 1)
