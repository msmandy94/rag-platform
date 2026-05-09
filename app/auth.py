"""Tenant API key authentication.

Each tenant has a single bearer token; we store only its SHA-256 hash.
Real prod would issue rotatable, scoped keys per principal — see TRADEOFFS.md.
"""
import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from app.db import pool


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    # 32 bytes -> 43-char URL-safe token, prefixed for grep-ability in logs.
    return "rag_" + secrets.token_urlsafe(32)


@dataclass
class Tenant:
    id: str
    name: str
    rate_limit_query_rpm: int
    rate_limit_ingest_rpm: int
    monthly_query_quota: int
    monthly_ingest_quota: int


async def require_tenant(
    authorization: str | None = Header(default=None),
) -> Tenant:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    h = hash_api_key(token)
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, name, rate_limit_query_rpm, rate_limit_ingest_rpm,
                   monthly_query_quota, monthly_ingest_quota
            FROM tenants WHERE api_key_hash = $1
            """,
            h,
        )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return Tenant(**dict(row))


def require_admin(authorization: str | None = Header(default=None)) -> None:
    from app.config import get_settings

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing admin token")
    token = authorization.split(" ", 1)[1].strip()
    if token != get_settings().ADMIN_TOKEN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad admin token")
