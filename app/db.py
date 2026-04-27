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
"""

import asyncpg
from app.config import settings

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
        async with conn.transaction():
            await conn.execute(...)
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Did startup run?")
    return await _pool.acquire()


async def release_conn(conn: asyncpg.Connection) -> None:
    """Return a borrowed connection back to the pool."""
    if _pool:
        await _pool.release(conn)
