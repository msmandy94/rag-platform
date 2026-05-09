"""Ingestion pipeline: queue claim → parse → chunk → embed → upsert.

The worker runs as an asyncio background task in the API process. It claims
queued jobs with `FOR UPDATE SKIP LOCKED`, processes them, and either marks
them done, retries with backoff, or routes to the DLQ after max_attempts.
"""
import asyncio
import hashlib

import structlog

from app.chunker import chunk_text
from app.config import get_settings
from app.db import pool
from app.embed import embed_texts
from app.parsers import extract_text
from app.qdrant import upsert_chunks

log = structlog.get_logger(__name__)


def content_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def enqueue_document(
    tenant_id: str,
    filename: str,
    mime_type: str,
    payload: bytes,
) -> tuple[str, bool]:
    """Idempotent: same (tenant, content_hash) returns the existing document."""
    h = content_hash(payload)
    async with pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id::text, status FROM documents WHERE tenant_id = $1 AND content_hash = $2",
                tenant_id,
                h,
            )
            if existing:
                return existing["id"], False

            doc = await conn.fetchrow(
                """
                INSERT INTO documents (tenant_id, filename, mime_type, size_bytes, content_hash, status)
                VALUES ($1, $2, $3, $4, $5, 'pending')
                RETURNING id::text
                """,
                tenant_id,
                filename,
                mime_type,
                len(payload),
                h,
            )
            await conn.execute(
                """
                INSERT INTO ingest_jobs (tenant_id, document_id, payload, max_attempts)
                VALUES ($1, $2, $3, $4)
                """,
                tenant_id,
                doc["id"],
                payload,
                get_settings().WORKER_MAX_RETRIES,
            )
    return doc["id"], True


async def _claim_job(conn) -> dict | None:
    row = await conn.fetchrow(
        """
        UPDATE ingest_jobs
        SET status = 'running', locked_until = now() + interval '5 minutes',
            attempts = attempts + 1, updated_at = now()
        WHERE id = (
            SELECT id FROM ingest_jobs
            WHERE status = 'queued' OR (status = 'running' AND locked_until < now())
            ORDER BY id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, tenant_id::text, document_id::text, payload, attempts, max_attempts
        """
    )
    return dict(row) if row else None


async def _process_job(job: dict) -> None:
    tenant_id = job["tenant_id"]
    document_id = job["document_id"]
    async with pool().acquire() as conn:
        doc = await conn.fetchrow(
            "SELECT mime_type, filename FROM documents WHERE id = $1",
            document_id,
        )
    if doc is None:
        raise RuntimeError("document missing for job")

    text = extract_text(job["payload"], doc["mime_type"])
    if not text.strip():
        raise ValueError("no extractable text")

    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("no chunks produced")

    vectors = await embed_texts([c.text for c in chunks])

    # Insert chunks; the chunk UUID becomes the Qdrant point id.
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM chunks WHERE document_id = $1",
                document_id,
            )
            inserted = await conn.fetch(
                """
                INSERT INTO chunks (tenant_id, document_id, chunk_index, text)
                SELECT $1, $2, x.idx, x.txt
                FROM unnest($3::int[], $4::text[]) AS x(idx, txt)
                RETURNING id::text, chunk_index
                """,
                tenant_id,
                document_id,
                [c.index for c in chunks],
                [c.text for c in chunks],
            )

    points = []
    for row, vec, chunk in zip(inserted, vectors, chunks):
        points.append(
            (
                row["id"],
                vec,
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "chunk_index": chunk.index,
                    "filename": doc["filename"],
                },
            )
        )
    await upsert_chunks(tenant_id, points)

    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE documents SET status = 'indexed', chunk_count = $2,
                                     updated_at = now(), error = NULL
                WHERE id = $1
                """,
                document_id,
                len(chunks),
            )
            await conn.execute(
                "UPDATE ingest_jobs SET status = 'done', updated_at = now() WHERE id = $1",
                job["id"],
            )
            # cost tracking: embed_chunks counts chunks embedded
            await conn.execute(
                """
                INSERT INTO usage_events (tenant_id, kind, provider, embed_chunks, latency_ms)
                VALUES ($1, 'ingest', 'local-bge', $2, 0)
                """,
                tenant_id,
                len(chunks),
            )


async def _fail_job(job: dict, err: Exception) -> None:
    log.warning("ingest.job_failed", job_id=job["id"], attempts=job["attempts"], err=str(err))
    async with pool().acquire() as conn:
        async with conn.transaction():
            if job["attempts"] >= job["max_attempts"]:
                await conn.execute(
                    """
                    UPDATE ingest_jobs SET status = 'failed', last_error = $2, updated_at = now()
                    WHERE id = $1
                    """,
                    job["id"],
                    str(err)[:1000],
                )
                await conn.execute(
                    """
                    UPDATE documents SET status = 'failed', error = $2, updated_at = now()
                    WHERE id = $1
                    """,
                    job["document_id"],
                    str(err)[:1000],
                )
                await conn.execute(
                    """
                    INSERT INTO dlq (tenant_id, document_id, job_id, reason, attempts, last_error)
                    VALUES ($1, $2, $3, 'max_attempts_exceeded', $4, $5)
                    """,
                    job["tenant_id"],
                    job["document_id"],
                    job["id"],
                    job["attempts"],
                    str(err)[:1000],
                )
            else:
                # Re-queue with backoff via locked_until in the past.
                await conn.execute(
                    """
                    UPDATE ingest_jobs
                    SET status = 'queued', last_error = $2, updated_at = now(),
                        locked_until = now() + (interval '5 seconds' * $3)
                    WHERE id = $1
                    """,
                    job["id"],
                    str(err)[:1000],
                    job["attempts"] ** 2,
                )


async def worker_loop(stop_event: asyncio.Event) -> None:
    interval = get_settings().WORKER_POLL_INTERVAL_SECONDS
    while not stop_event.is_set():
        try:
            async with pool().acquire() as conn:
                async with conn.transaction():
                    job = await _claim_job(conn)
            if job is None:
                await asyncio.sleep(interval)
                continue
            try:
                await _process_job(job)
                log.info("ingest.job_done", job_id=job["id"], document_id=job["document_id"])
            except Exception as e:
                await _fail_job(job, e)
        except Exception as e:
            log.exception("ingest.worker_error", err=str(e))
            await asyncio.sleep(interval)
