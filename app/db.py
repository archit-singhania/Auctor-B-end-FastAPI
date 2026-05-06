"""
app/db.py — asyncpg connection pool, shared across the entire app.

How it works:
  - On startup (lifespan), FastAPI calls create_pool() which opens a pool
    of 2–10 PostgreSQL connections using asyncpg.
  - Every request that needs the DB calls `get_conn()` which borrows a
    connection from the pool for the duration of the request.
  - On shutdown, the pool is gracefully closed.
  - search_path is set to the auctor schema so all queries can use bare
    table names without the "auctor." prefix.
  - run_migrations() auto-creates the schema + tables on first deploy.
"""

import logging
import pathlib
import asyncpg
from app.config import settings

logger = logging.getLogger(__name__)

# Module-level pool — initialised once in lifespan
_pool: asyncpg.Pool | None = None


async def create_pool() -> None:
    """Create the global connection pool. Called once at app startup."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.db_dsn,
        min_size=2,
        max_size=10,
        # Set search_path so bare table names resolve to the auctor schema
        server_settings={"search_path": settings.db_schema},
    )
    await run_migrations()


async def run_migrations() -> None:
    """
    Auto-create schema and tables on first deploy.
    Reads schema.sql from the project root and executes it.
    All statements use CREATE IF NOT EXISTS / ON CONFLICT DO NOTHING
    so this is safe to run on every startup.
    """
    schema_path = pathlib.Path(__file__).parent.parent / "schema.sql"
    if not schema_path.exists():
        logger.warning("schema.sql not found at %s — skipping migrations", schema_path)
        return

    sql = schema_path.read_text(encoding="utf-8")
    try:
        conn = await _pool.acquire()  # type: ignore[union-attr]
        try:
            await conn.execute(sql)
            logger.info("DB migrations applied successfully")
        finally:
            await _pool.release(conn)  # type: ignore[union-attr]
    except Exception as exc:
        logger.error("DB migration failed: %s", exc)
        # Don't crash startup — app can still serve requests that don't need DB


async def close_pool() -> None:
    """Close the pool gracefully. Called once at app shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_conn() -> asyncpg.Connection:
    """
    Borrow a connection from the pool.
    Usage (in a router/service):
        conn = await get_conn()
        try:
            await conn.execute(...)
        finally:
            await release_conn(conn)
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Did startup run?")
    return await _pool.acquire()


async def release_conn(conn: asyncpg.Connection) -> None:
    """Return a borrowed connection back to the pool."""
    if _pool:
        await _pool.release(conn)
