"""Auctor API entrypoint: explicit origins, durable jobs and dependency readiness."""
import asyncio
from contextlib import asynccontextmanager, suppress
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.db import create_pool, close_pool, get_conn, release_conn
from app.domain import WEIGHTS, score_from
from app.platform import router, job_worker

logger=logging.getLogger(__name__)
@asynccontextmanager
async def lifespan(app):
    settings.prepare_storage()
    score_from([],[],[],{k:getattr(settings,'weight_'+k) for k in WEIGHTS})
    await create_pool()
    worker=asyncio.create_task(job_worker())
    from app.workbench import snapshot_worker
    evidence_worker=asyncio.create_task(snapshot_worker())
    from app.verification import lifecycle_worker
    verification_worker=asyncio.create_task(lifecycle_worker())
    from app.integrations import outbox_worker
    integration_worker=asyncio.create_task(outbox_worker())
    try:
        yield
    finally:
        worker.cancel()
        evidence_worker.cancel()
        verification_worker.cancel()
        integration_worker.cancel()
        with suppress(asyncio.CancelledError): await worker
        with suppress(asyncio.CancelledError): await evidence_worker
        with suppress(asyncio.CancelledError): await verification_worker
        with suppress(asyncio.CancelledError): await integration_worker
        await close_pool()

app=FastAPI(title='Auctor evidence platform',version='2.0.0',lifespan=lifespan,description='Owned CV evidence, server-graded skill challenges and explainable provenance.')
app.add_middleware(CORSMiddleware,allow_origins=settings.cors_origins,allow_methods=['GET','POST','PUT','PATCH','DELETE'],allow_headers=['Authorization','Content-Type'],expose_headers=['Content-Disposition'],allow_credentials=False)

@app.middleware('http')
async def security_headers(request:Request,call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Cache-Control']='no-store'
    return response

app.include_router(router)
from app.career import router as career_router
app.include_router(career_router)
from app.role_fit import router as role_router
app.include_router(role_router)
from app.workbench import router as workbench_router
app.include_router(workbench_router)
from app.presentations import router as presentation_router
app.include_router(presentation_router)
from app.verification import router as verification_router
app.include_router(verification_router)
from app.integrations import router as integration_router
app.include_router(integration_router)
@app.get('/health')
async def health():
    c=await get_conn()
    try:
        await c.fetchval('SELECT 1')
        version=await c.fetchval('SELECT MAX(version) FROM schema_versions')
        return {'status':'ready','database':'connected','schema_version':version,'service':'auctor-api','environment':settings.app_env,'storage':settings.storage_mode}
    finally: await release_conn(c)

@app.get('/ping')
async def ping(): return {'pong':True}
