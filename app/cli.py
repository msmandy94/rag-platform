"""CLI entry point: api, worker (standalone), migrate, seed."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import typer

from app.auth import generate_api_key, hash_api_key
from app.config import get_settings

app = typer.Typer(add_completion=False)


@app.command()
def api(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Run the FastAPI server (worker runs in-process)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port or settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        access_log=False,
    )


@app.command()
def worker() -> None:
    """Run a standalone worker (no API). For prod where api/worker are split."""
    from app.db import close_pool, init_pool
    from app.embed import load_model
    from app.ingest import worker_loop

    async def _run() -> None:
        await init_pool()
        load_model()
        stop = asyncio.Event()
        try:
            await worker_loop(stop)
        finally:
            await close_pool()

    asyncio.run(_run())


@app.command()
def migrate(path: str = "migrations") -> None:
    """Apply all .sql files in `migrations/` (alphabetical) to DATABASE_URL."""
    settings = get_settings()

    async def _run() -> None:
        files = sorted(Path(path).glob("*.sql"))
        if not files:
            typer.echo("no migration files found")
            return
        conn = await asyncpg.connect(settings.DATABASE_URL, statement_cache_size=0)
        try:
            for f in files:
                typer.echo(f"applying {f.name} ...")
                await conn.execute(f.read_text())
            typer.echo("migrations done")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def seed_tenant(name: str) -> None:
    """Create a tenant and print its API key. Useful for grading."""
    settings = get_settings()

    async def _run() -> None:
        conn = await asyncpg.connect(settings.DATABASE_URL, statement_cache_size=0)
        try:
            api_key = generate_api_key()
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (name, api_key_hash) VALUES ($1, $2)
                RETURNING id::text, name
                """,
                name,
                hash_api_key(api_key),
            )
            typer.echo(f"tenant_id: {row['id']}")
            typer.echo(f"name:      {row['name']}")
            typer.echo(f"api_key:   {api_key}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command()
def seed_demo() -> None:
    """Insert a demo tenant + a small text document for smoke testing."""
    settings = get_settings()

    async def _run() -> None:
        conn = await asyncpg.connect(settings.DATABASE_URL, statement_cache_size=0)
        try:
            api_key = os.environ.get("DEMO_API_KEY") or generate_api_key()
            existing = await conn.fetchrow(
                "SELECT id::text FROM tenants WHERE name = 'demo'"
            )
            if existing:
                typer.echo(f"demo tenant exists: {existing['id']}")
                typer.echo("Reset api key:")
                await conn.execute(
                    "UPDATE tenants SET api_key_hash = $1 WHERE id = $2",
                    hash_api_key(api_key),
                    existing["id"],
                )
                tenant_id = existing["id"]
            else:
                row = await conn.fetchrow(
                    "INSERT INTO tenants (name, api_key_hash) VALUES ('demo', $1) RETURNING id::text",
                    hash_api_key(api_key),
                )
                tenant_id = row["id"]
            typer.echo(f"tenant_id: {tenant_id}")
            typer.echo(f"api_key:   {api_key}")
        finally:
            await conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
