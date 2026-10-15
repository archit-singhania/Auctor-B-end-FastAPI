"""Reviewed recruiter walkthroughs with narrowly scoped, expiring public shares."""
import asyncio
import threading
from pathlib import Path
import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone, timedelta
from io import BytesIO
from xml.sax.saxutils import escape
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from app.platform import current_user, db, decode, cv_data
from app.career import public_story

FONT_LOCK=threading.Lock()
router=APIRouter(prefix='/api/presentations',tags=['Recruiter presentations'])

class Presentation(BaseModel):
    revision:int=Field(default=0,ge=0)
    title:str=Field(default='My work, with evidence',min_length=1,max_length=160)
    introduction:str=Field(default='',max_length=2000)
    private_notes:str=Field(default='',max_length=4000)
    milestone_ids:list[str]=Field(default_factory=list,max_length=100)
    project_names:list[str]=Field(default_factory=list,max_length=50)
    evidence_ids:list[str]=Field(default_factory=list,max_length=50)
    role_run_id:str|None=Field(default=None,max_length=80)

async def config(c,uid):
    found=await c.fetchrow('SELECT revision,data FROM recruiter_presentations WHERE user_id=$1',uid)
    return {**decode(found['data']),'revision':found['revision']} if found else Presentation().model_dump()

async def projection(c,uid,value):
    user=await c.fetchrow('SELECT display_name,handle FROM users WHERE id=$1',uid)
    story=await public_story(c,uid);cv=await cv_data(c,uid)
    evidence=[dict(r) for r in await c.fetch("SELECT id,title,kind,status,url FROM evidence WHERE user_id=$1 AND status='verified' AND eligibility_state='current' AND (review_due_at IS NULL OR review_due_at>NOW()) ORDER BY id",uid)]
    chosen={r['id']:r for r in evidence if r['id'] in value['evidence_ids']}
    milestones=[{**m,'evidence':[e for e in m['evidence'] if e['id'] in chosen]} for m in story['milestones'] if m['id'] in value['milestone_ids']]
    projects=[{k:p.get(k,'') for k in ('name','description','tech_stack')} for p in cv.get('projects',[]) if p['name'] in value['project_names']]
    role=None
    if value.get('role_run_id'):
        run=await c.fetchval('SELECT result FROM role_fit_runs WHERE id=$1 AND user_id=$2',value['role_run_id'],uid)
        if run is None:raise HTTPException(404,'Role snapshot unavailable')
        result=decode(run);role={'formula_version':result['formula_version'],'scope':'Selected public evidence only. Private requirements, job description and source details are omitted. This can differ from the private match.','contributions':[]}
        badges={r['badge_id'] for r in await c.fetch('SELECT DISTINCT badge_id FROM attempts WHERE user_id=$1 AND passed=TRUE',uid)}
        for r in result['contributions']:
            sources=[s for s in r['sources'] if (s['kind']=='evidence' and s['id'] in chosen) or (s['kind']=='assessment' and s['id'] in badges)]
            role['contributions'].append({'skill':r['skill'],'weight':r['weight'],'status':'publicly evidenced' if sources else 'not publicly evidenced','sources':sources})
    return {'profile':dict(user),'title':value['title'],'introduction':value['introduction'],'career_story':{'milestones':milestones},'projects':projects,'evidence':list(chosen.values()),'role_fit':role,'scope':'Candidate-written narrative and selected sources; not a hiring or competency guarantee.'}

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

@router.get('')
async def get_config(user=Depends(current_user)):
    async with db() as c:return await config(c,user['id'])

@router.put('')
async def save(body:Presentation,user=Depends(current_user)):
    async with db() as c:
        async with c.transaction():
            await c.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE',user['id'])
            old=await config(c,user['id'])
            if old['revision']!=body.revision:raise HTTPException(409,'Presentation changed; reload before replacing the saved draft')
            cv=await cv_data(c,user['id']);story=await c.fetchval('SELECT milestones FROM career_stories WHERE user_id=$1',user['id'])
            if set(body.project_names)-{p['name'] for p in cv.get('projects',[])} or set(body.milestone_ids)-{m['id'] for m in decode(story or '[]')}:raise HTTPException(404,'Selected project or milestone unavailable')
            ids={r['id'] for r in await c.fetch('SELECT id FROM evidence WHERE user_id=$1 AND id=ANY($2::text[])',user['id'],body.evidence_ids)}
            if set(body.evidence_ids)!=ids:raise HTTPException(404,'Selected evidence unavailable')
            value=body.model_dump();await projection(c,user['id'],value)
            value['revision']+=1
            await c.execute('INSERT INTO recruiter_presentations(user_id,revision,data) VALUES($1,$2,$3::jsonb) ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,data=EXCLUDED.data',user['id'],value['revision'],json.dumps(value))
            return value

@router.get('/preview')
async def preview(user=Depends(current_user)):
    async with db() as c:
        value=await config(c,user['id']);data=await projection(c,user['id'],value)
    return {'revision':value['revision'],'preview_sha256':digest(data),'data':data}

class Publish(BaseModel):
    revision:int=Field(ge=0)
    preview_sha256:str=Field(min_length=64,max_length=64)
    confirm_public:bool=False
    expires_days:int=Field(default=7,ge=1,le=90)

@router.post('/shares',status_code=201)
async def publish(body:Publish,user=Depends(current_user)):
    if not body.confirm_public:raise HTTPException(400,'Review and confirm the public presentation before sharing')
    async with db() as c:
        async with c.transaction(isolation='repeatable_read'):
            value=await config(c,user['id']);data=await projection(c,user['id'],value)
            if value['revision']!=body.revision or digest(data)!=body.preview_sha256:raise HTTPException(409,'Presentation sources changed. Review the updated public preview')
            sid=secrets.token_urlsafe(24);expires=datetime.now(timezone.utc)+timedelta(days=body.expires_days)
            await c.execute("INSERT INTO shares(id,user_id,kind,expires_at) VALUES($1,$2,'presentation',$3)",sid,user['id'],expires)
            data['snapshot_at']=datetime.now(timezone.utc).isoformat()
            await c.execute('INSERT INTO recruiter_presentation_shares(share_id,data) VALUES($1,$2::jsonb)',sid,json.dumps(data))
    from app.config import settings
    return {'id':sid,'expires_at':expires,'url':settings.web_url.rstrip('/')+'/#/presentation/'+sid}

async def shared(c,sid):
    found=await c.fetchrow("SELECT s.user_id,p.data FROM shares s JOIN recruiter_presentation_shares p ON p.share_id=s.id WHERE s.id=$1 AND s.kind='presentation' AND s.revoked=FALSE AND s.expires_at>NOW()",sid)
    if not found:raise HTTPException(404,'Presentation expired, revoked or unavailable')
    data=copy.deepcopy(decode(found['data']))
    allowed={r['id'] for r in await c.fetch("SELECT id FROM evidence WHERE user_id=$1 AND status='verified' AND eligibility_state='current' AND (review_due_at IS NULL OR review_due_at>NOW())",found['user_id'])}
    data['evidence']=[e for e in data['evidence'] if e['id'] in allowed]
    for m in data['career_story']['milestones']:m['evidence']=[e for e in m['evidence'] if e['id'] in allowed]
    if data['role_fit']:
        for r in data['role_fit']['contributions']:
            r['sources']=[s for s in r['sources'] if s['kind']=='assessment' or s['id'] in allowed]
            r['status']='publicly evidenced' if r['sources'] else 'not publicly evidenced'
    return data

@router.get('/shared/{sid}')
async def public_presentation(sid:str):
    async with db() as c:return await shared(c,sid)

def brief_pdf(data):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with FONT_LOCK:
        for name in ('Inter','Newsreader'):
            if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(Path(__file__).parent/'assets'/'fonts'/(name+'.ttf'))))
    buffer=BytesIO();doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=48,leftMargin=48,topMargin=52,bottomMargin=48,title='Auctor interview brief')
    body=ParagraphStyle('body',fontName='Inter',fontSize=10,leading=15,spaceAfter=9,textColor=colors.HexColor('#263c3c'),wordWrap='CJK')
    title=ParagraphStyle('title',parent=body,fontName='Newsreader',fontSize=25,leading=30,spaceAfter=16)
    heading=ParagraphStyle('heading',parent=body,fontName='Helvetica-Bold',fontSize=13,leading=18,spaceBefore=16,keepWithNext=True)
    def text(v):return escape(str(v)).replace('\n','<br/>')
    story=[Paragraph(text(data['title']),title),Paragraph(text(data['profile']['display_name'])+' · @'+text(data['profile']['handle']),body),Paragraph(text(data['introduction']),body),Paragraph(text(data['scope']),body)]
    for m in data['career_story']['milestones']:story.extend([Paragraph(text(m['date'])+' · '+text(m['title']),heading),Paragraph(text(m['narrative']),body),Paragraph('Candidate-written narrative',body)])
    for p in data['projects']:story.extend([Paragraph(text(p['name']),heading),Paragraph(text(p['description']),body)])
    if data['role_fit']:
        story.extend([Paragraph('Role-fit explanations',heading),Paragraph(text(data['role_fit']['scope']),body)])
        for r in data['role_fit']['contributions']:story.append(Paragraph(text(r['skill'])+' · '+text(r['status']),body))
    story.append(Paragraph('Selected evidence',heading))
    for e in data['evidence']:story.append(Paragraph(text(e['title'])+' · '+text(e['kind'])+' · '+text(e['status']),body))
    def footer(canvas,document):
        canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#526568'));canvas.drawString(48,28,'AUCTOR · Interview brief');canvas.drawRightString(A4[0]-48,28,str(document.page))
    doc.build(story,onFirstPage=footer,onLaterPages=footer);return buffer.getvalue()

@router.get('/shared/{sid}/brief.pdf')
async def brief(sid:str):
    async with db() as c:data=await shared(c,sid)
    return Response(await asyncio.to_thread(brief_pdf,data),media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="auctor-interview-brief.pdf"'})
