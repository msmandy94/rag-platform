"""Per-tenant token bucket persisted in Postgres.

Trade-off: Postgres-based rate limiting adds DB round-trip per request. For
real scale (>1k RPS/tenant) we'd front this with Redis. See TRADEOFFS.md.
"""
import time

from fastapi import HTTPException, status

from app.db import pool


async def check_and_consume(tenant_id: str, action: str, rpm: int) -> None:
    """Token bucket: capacity = rpm, refill = rpm tokens / 60 sec.

    One UPSERT round-trip per call. Returns 429 with Retry-After when empty.
    """
    capacity = float(rpm)
    refill_per_sec = capacity / 60.0
    now = time.time()
    async with pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT tokens, EXTRACT(EPOCH FROM refilled_at) AS refilled_at_epoch
                FROM rate_buckets
                WHERE tenant_id = $1 AND action = $2
                FOR UPDATE
                """,
                tenant_id,
                action,
            )
            if row is None:
                tokens = capacity - 1.0
                await conn.execute(
                    """
                    INSERT INTO rate_buckets (tenant_id, action, tokens, refilled_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (tenant_id, action) DO NOTHING
                    """,
                    tenant_id,
                    action,
                    tokens,
                )
                return

            elapsed = max(0.0, now - float(row["refilled_at_epoch"]))
            tokens = min(capacity, float(row["tokens"]) + elapsed * refill_per_sec)
            if tokens < 1.0:
                # Time until 1 full token is available
                retry_after = max(1, int((1.0 - tokens) / refill_per_sec))
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="rate limit exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
            tokens -= 1.0
            await conn.execute(
                """
                UPDATE rate_buckets SET tokens = $3, refilled_at = now()
                WHERE tenant_id = $1 AND action = $2
                """,
                tenant_id,
                action,
                tokens,
            )
