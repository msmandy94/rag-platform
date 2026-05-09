"""Qdrant client wrapper: collection-per-tenant for hard isolation.

Trade-off: collection-per-tenant gives strong isolation and per-tenant indexing
choices, but does not scale beyond ~1k tenants on a single cluster. For larger
multi-tenancy we'd shard tenants across clusters or switch to filtered single
collection with payload-level isolation. See TRADEOFFS.md.
"""
import asyncio
from typing import Iterable

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams, Filter, FieldCondition, MatchValue

from app.config import get_settings

_client: AsyncQdrantClient | None = None
_known_collections: set[str] = set()
_lock = asyncio.Lock()


def _collection_name(tenant_id: str) -> str:
    return f"t_{tenant_id.replace('-', '')}"


async def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncQdrantClient(
            url=s.QDRANT_URL,
            api_key=s.QDRANT_API_KEY or None,
            timeout=30,
        )
    return _client


async def ensure_collection(tenant_id: str) -> str:
    name = _collection_name(tenant_id)
    if name in _known_collections:
        return name
    async with _lock:
        if name in _known_collections:
            return name
        c = await client()
        existing = {col.name for col in (await c.get_collections()).collections}
        if name not in existing:
            await c.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=get_settings().EMBED_DIM, distance=Distance.COSINE
                ),
            )
        _known_collections.add(name)
        return name


async def upsert_chunks(
    tenant_id: str,
    points: Iterable[tuple[str, list[float], dict]],
) -> int:
    """points: (chunk_id_uuid, vector, payload). chunk_id is also the Qdrant point id."""
    name = await ensure_collection(tenant_id)
    structs = [
        PointStruct(id=chunk_id, vector=vec, payload=payload)
        for chunk_id, vec, payload in points
    ]
    if not structs:
        return 0
    await (await client()).upsert(collection_name=name, points=structs, wait=True)
    return len(structs)


async def search(
    tenant_id: str,
    query_vec: list[float],
    top_k: int = 20,
    document_ids: list[str] | None = None,
) -> list[tuple[str, float, dict]]:
    """Returns [(chunk_id, score, payload)]. Tenant scope is implicit (collection)."""
    name = await ensure_collection(tenant_id)
    qfilter = None
    if document_ids:
        qfilter = Filter(
            should=[
                FieldCondition(key="document_id", match=MatchValue(value=did))
                for did in document_ids
            ]
        )
    res = await (await client()).search(
        collection_name=name,
        query_vector=query_vec,
        limit=top_k,
        query_filter=qfilter,
        with_payload=True,
    )
    return [(str(p.id), float(p.score), p.payload or {}) for p in res]


async def delete_document(tenant_id: str, document_id: str) -> None:
    name = await ensure_collection(tenant_id)
    await (await client()).delete(
        collection_name=name,
        points_selector=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
    )
