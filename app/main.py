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
    score_from([],[],[],{k:getattr(settings,'weight_'+k) for k in WEIGHTS})
    await create_pool()
    worker=asyncio.create_task(job_worker())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError): await worker
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
@app.get('/health')
async def health():
    c=await get_conn()
    try:
        await c.fetchval('SELECT 1')
        version=await c.fetchval('SELECT MAX(version) FROM schema_versions')
        return {'status':'ready','database':'connected','schema_version':version,'service':'auctor-api'}
    finally: await release_conn(c)

@app.get('/ping')
async def ping(): return {'pong':True}
