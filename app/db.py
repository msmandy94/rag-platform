from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0,  # Supabase pooler is in transaction mode
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


@asynccontextmanager
async def tenant_tx(tenant_id: str | None) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection and bind it to a tenant for RLS.

    Sets the `app.tenant_id` GUC for the duration of the transaction.
    Pass tenant_id=None for admin/worker paths that need to bypass RLS-by-tenant
    (RLS policies use current_setting and treat NULL as no match — admin paths
    must use connections without RLS-restricted tables, or the service_role).
    """
    p = pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            if tenant_id is not None:
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", tenant_id
                )
            yield conn
