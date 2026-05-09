"""HTTP API surface."""
from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth import Tenant, generate_api_key, hash_api_key, require_admin, require_tenant
from app.config import get_settings
from app.db import pool
from app.ingest import enqueue_document
from app.parsers import SUPPORTED_MIME_TYPES
from app.ratelimit import check_and_consume
from app.retrieval import answer_question

log = structlog.get_logger(__name__)
router = APIRouter()


# ---------- Health -----------------------------------------------------

@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------- Frontend bootstrap config ----------------------------------

@router.get("/config.js", include_in_schema=False)
async def config_js() -> Response:
    """Public bootstrap config consumed by the SPA at load time.

    Surfaces only non-secret values: GA measurement ID and the demo tenant key
    (the latter is intentionally public — that's what makes the demo a demo).
    """
    s = get_settings()
    js = (
        f"window.APP_CONFIG = "
        f'{{"gaMeasurementId": {json.dumps(s.GA_MEASUREMENT_ID)}, '
        f'"demoApiKey": {json.dumps(s.DEMO_API_KEY)}}};'
    )
    return Response(content=js, media_type="application/javascript", headers={
        "Cache-Control": "no-store",
    })


@router.get("/api", include_in_schema=False)
async def api_index() -> dict:
    return {
        "name": "rag-platform",
        "endpoints": [
            "POST /v1/documents",
            "GET  /v1/documents/{id}",
            "POST /v1/query",
            "GET  /v1/usage",
            "POST /admin/tenants",
            "GET  /admin/tenants",
        ],
    }


# ---------- Admin: tenant management ------------------------------------

class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    rate_limit_query_rpm: int | None = None
    rate_limit_ingest_rpm: int | None = None


class TenantCreated(BaseModel):
    tenant_id: str
    name: str
    api_key: str  # returned once, never again


@router.post("/admin/tenants", response_model=TenantCreated, dependencies=[Depends(require_admin)])
async def create_tenant(body: TenantCreate) -> TenantCreated:
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)
    async with pool().acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (name, api_key_hash, rate_limit_query_rpm, rate_limit_ingest_rpm)
                VALUES ($1, $2,
                        COALESCE($3, 60),
                        COALESCE($4, 300))
                RETURNING id::text, name
                """,
                body.name,
                api_key_hash,
                body.rate_limit_query_rpm,
                body.rate_limit_ingest_rpm,
            )
        except Exception as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return TenantCreated(tenant_id=row["id"], name=row["name"], api_key=api_key)


@router.get("/admin/tenants", dependencies=[Depends(require_admin)])
async def list_tenants() -> dict:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id::text AS id,
                   t.name,
                   t.rate_limit_query_rpm,
                   t.rate_limit_ingest_rpm,
                   to_char(t.created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
                   COALESCE(d.cnt, 0) AS document_count,
                   COALESCE(u.queries, 0) AS queries_30d,
                   COALESCE(u.cost_usd_micro, 0) AS cost_usd_micro_30d
            FROM tenants t
            LEFT JOIN (
                SELECT tenant_id, COUNT(*)::int AS cnt FROM documents GROUP BY tenant_id
            ) d ON d.tenant_id = t.id
            LEFT JOIN (
                SELECT tenant_id,
                       COUNT(*) FILTER (WHERE kind='query')::int AS queries,
                       COALESCE(SUM(cost_usd_micro), 0) AS cost_usd_micro
                FROM usage_events
                WHERE created_at > now() - interval '30 days'
                GROUP BY tenant_id
            ) u ON u.tenant_id = t.id
            ORDER BY t.created_at DESC
            """
        )
    return {"tenants": [dict(r) for r in rows]}


# ---------- Documents ---------------------------------------------------

class DocumentOut(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    chunk_count: int
    error: str | None
    created_at: str


@router.post("/v1/documents", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    metadata: str | None = Form(default=None),
    tenant: Tenant = Depends(require_tenant),
) -> DocumentOut:
    await check_and_consume(tenant.id, "ingest", tenant.rate_limit_ingest_rpm)

    mime_type = file.content_type or ""
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported mime type: {mime_type}. supported: {sorted(SUPPORTED_MIME_TYPES)}",
        )
    payload = await file.read()
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty payload")

    if metadata:
        try:
            json.loads(metadata)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "metadata must be JSON")

    document_id, _new = await enqueue_document(
        tenant_id=tenant.id,
        filename=file.filename or "unnamed",
        mime_type=mime_type,
        payload=payload,
    )
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, filename, mime_type, size_bytes, status, chunk_count, error,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at
            FROM documents WHERE id = $1
            """,
            document_id,
        )
    return DocumentOut(**dict(row))


@router.get("/v1/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str, tenant: Tenant = Depends(require_tenant)) -> DocumentOut:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, filename, mime_type, size_bytes, status, chunk_count, error,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at
            FROM documents WHERE id = $1 AND tenant_id = $2
            """,
            document_id,
            tenant.id,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return DocumentOut(**dict(row))


class DocumentsList(BaseModel):
    documents: list[DocumentOut]
    count: int
    limit: int
    offset: int


@router.get("/v1/documents", response_model=DocumentsList)
async def list_documents(
    tenant: Tenant = Depends(require_tenant),
    limit: int = 100,
    offset: int = 0,
) -> DocumentsList:
    """Paginated list of the calling tenant's documents, newest first.

    Hard ceiling on `limit` keeps a forgetful client from fetching 10M rows.
    Real prod would expose a cursor; LIMIT/OFFSET is fine for the demo.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, filename, mime_type, size_bytes, status, chunk_count, error,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at
            FROM documents
            WHERE tenant_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            tenant.id,
            limit,
            offset,
        )
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE tenant_id = $1", tenant.id
        )
    return DocumentsList(
        documents=[DocumentOut(**dict(r)) for r in rows],
        count=int(cnt or 0),
        limit=limit,
        offset=offset,
    )


class DocumentChunk(BaseModel):
    index: int
    text: str


class DocumentPreview(BaseModel):
    id: str
    filename: str
    mime_type: str
    status: str
    chunk_count: int
    chunks: list[DocumentChunk]


@router.get("/v1/documents/{document_id}/text", response_model=DocumentPreview)
async def get_document_text(
    document_id: str, tenant: Tenant = Depends(require_tenant)
) -> DocumentPreview:
    """Return the document's extracted text, chunk by chunk.

    Reading from `chunks` (the indexed text we actually retrieve from) gives
    a faithful preview of what the retriever sees, including any extraction
    artifacts. Cheap: ~10s of chunks at ~800 chars each.
    """
    async with pool().acquire() as conn:
        doc = await conn.fetchrow(
            """
            SELECT id::text, filename, mime_type, status, chunk_count
            FROM documents WHERE id = $1 AND tenant_id = $2
            """,
            document_id,
            tenant.id,
        )
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        rows = await conn.fetch(
            """
            SELECT chunk_index, text FROM chunks
            WHERE document_id = $1 AND tenant_id = $2
            ORDER BY chunk_index
            """,
            document_id,
            tenant.id,
        )
    return DocumentPreview(
        id=doc["id"],
        filename=doc["filename"],
        mime_type=doc["mime_type"],
        status=doc["status"],
        chunk_count=doc["chunk_count"],
        chunks=[DocumentChunk(index=r["chunk_index"], text=r["text"]) for r in rows],
    )


@router.delete("/v1/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document_route(
    document_id: str, tenant: Tenant = Depends(require_tenant)
) -> Response:
    """Delete a document and all of its chunks from Postgres + Qdrant.

    Order matters: we delete the vectors first, then the SQL rows. If Qdrant
    fails the SQL data survives and the user can retry. The reverse order
    can leave orphaned vectors that retrieval would happily score against
    chunks that no longer exist.

    We delete vectors by point ID (chunks.id == Qdrant point id), not by
    payload filter — Qdrant Cloud requires an indexed payload field for
    filter-based delete and we'd rather not maintain that index.
    """
    from app.qdrant import delete_chunks as qdrant_delete_chunks

    async with pool().acquire() as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM documents WHERE id = $1 AND tenant_id = $2",
            document_id,
            tenant.id,
        )
        if not owned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

        chunk_ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT id::text FROM chunks WHERE document_id = $1 AND tenant_id = $2",
                document_id,
                tenant.id,
            )
        ]

    try:
        await qdrant_delete_chunks(tenant.id, chunk_ids)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"vector store delete failed: {e}",
        )

    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM documents WHERE id = $1 AND tenant_id = $2",
            document_id,
            tenant.id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Query -------------------------------------------------------

class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)
    document_ids: list[str] | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[dict]
    provider: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    retrieved: int


@router.post("/v1/query", response_model=QueryResponse)
async def query(
    body: QueryRequest, tenant: Tenant = Depends(require_tenant)
) -> QueryResponse:
    await check_and_consume(tenant.id, "query", tenant.rate_limit_query_rpm)
    resp = await answer_question(
        tenant_id=tenant.id,
        question=body.question,
        top_k=body.top_k,
        document_ids=body.document_ids,
    )
    return QueryResponse(
        answer=resp.answer,
        citations=resp.citations,
        provider=resp.provider,
        latency_ms=resp.latency_ms,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        retrieved=resp.retrieved,
    )


# ---------- Usage / cost ------------------------------------------------

@router.get("/v1/usage")
async def usage(tenant: Tenant = Depends(require_tenant)) -> dict:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT kind, provider,
                   COUNT(*) AS events,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(embed_chunks), 0) AS embed_chunks,
                   COALESCE(SUM(cost_usd_micro), 0) AS cost_usd_micro
            FROM usage_events
            WHERE tenant_id = $1
              AND created_at > now() - interval '30 days'
            GROUP BY kind, provider
            ORDER BY kind, provider
            """,
            tenant.id,
        )
    return {
        "tenant_id": tenant.id,
        "window": "30d",
        "rows": [dict(r) for r in rows],
    }
