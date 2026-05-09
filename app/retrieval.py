"""Hybrid retrieval (BM25 + vector) with Reciprocal Rank Fusion.

Why RRF: it's parameter-free, robust to score-scale differences between BM25
and cosine, and matches what's used in production retrieval systems. A
re-ranker (Cohere / bge-reranker) on top would lift quality further; see
TRADEOFFS.md.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import structlog

from app.db import pool
from app.embed import embed_query
from app.llm import LLMResult, complete_with_failover, estimate_cost_micro_usd
from app.qdrant import search as vector_search

log = structlog.get_logger(__name__)


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    text: str
    score: float
    filename: str
    chunk_index: int


SYSTEM_PROMPT = (
    "You are a careful enterprise assistant. Answer using ONLY the provided context. "
    "Cite sources by their bracketed chunk number, e.g. [1] or [2,3]. "
    "If the context doesn't contain the answer, say so plainly. Be concise."
)


async def _bm25(tenant_id: str, query: str, top_k: int) -> list[tuple[str, float]]:
    sql = """
        SELECT id::text AS chunk_id,
               ts_rank(text_tsv, plainto_tsquery('english', $2)) AS rank
        FROM chunks
        WHERE tenant_id = $1
          AND text_tsv @@ plainto_tsquery('english', $2)
        ORDER BY rank DESC
        LIMIT $3
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, tenant_id, query, top_k)
    return [(r["chunk_id"], float(r["rank"])) for r in rows]


def _rrf_merge(
    rankings: list[list[tuple[str, float]]],
    k: int = 60,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


async def hybrid_retrieve(
    tenant_id: str,
    query: str,
    top_k: int = 8,
    document_ids: list[str] | None = None,
) -> list[Hit]:
    qvec_task = embed_query(query)
    bm25_task = _bm25(tenant_id, query, top_k=top_k * 3)
    qvec = await qvec_task
    bm25_hits = await bm25_task
    vec_hits = await vector_search(
        tenant_id, qvec, top_k=top_k * 3, document_ids=document_ids
    )

    fused = _rrf_merge(
        [bm25_hits, [(cid, score) for cid, score, _ in vec_hits]]
    )[:top_k]
    if not fused:
        return []

    chunk_ids = [cid for cid, _ in fused]
    score_by_id = dict(fused)

    sql = """
        SELECT c.id::text AS chunk_id, c.document_id::text AS document_id,
               c.text, c.chunk_index, d.filename
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.tenant_id = $1 AND c.id = ANY($2::uuid[])
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(sql, tenant_id, chunk_ids)

    by_id = {r["chunk_id"]: r for r in rows}
    hits: list[Hit] = []
    for cid in chunk_ids:
        r = by_id.get(cid)
        if r is None:
            continue
        hits.append(
            Hit(
                chunk_id=cid,
                document_id=r["document_id"],
                text=r["text"],
                score=score_by_id[cid],
                filename=r["filename"],
                chunk_index=r["chunk_index"],
            )
        )
    return hits


def _build_user_prompt(question: str, hits: list[Hit]) -> str:
    lines = ["Context:"]
    for i, h in enumerate(hits, start=1):
        lines.append(f"[{i}] (source: {h.filename}, chunk {h.chunk_index})\n{h.text}")
    lines.append("")
    lines.append(f"Question: {question}")
    lines.append("Answer with citations in [n] format.")
    return "\n\n".join(lines)


@dataclass
class RAGResponse:
    answer: str
    citations: list[dict]
    provider: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    retrieved: int


async def answer_question(
    tenant_id: str,
    question: str,
    top_k: int = 8,
    document_ids: list[str] | None = None,
) -> RAGResponse:
    start = time.perf_counter()
    hits = await hybrid_retrieve(
        tenant_id, question, top_k=top_k, document_ids=document_ids
    )
    if not hits:
        return RAGResponse(
            answer="I don't have any indexed content matching that question.",
            citations=[],
            provider="none",
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=0,
            output_tokens=0,
            retrieved=0,
        )
    user = _build_user_prompt(question, hits)
    result: LLMResult = await complete_with_failover(SYSTEM_PROMPT, user, max_tokens=600)

    citations = [
        {
            "n": i,
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "filename": h.filename,
            "chunk_index": h.chunk_index,
            "score": round(h.score, 6),
        }
        for i, h in enumerate(hits, start=1)
    ]
    cost = estimate_cost_micro_usd(result.provider, result.input_tokens, result.output_tokens)
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_events
                (tenant_id, kind, provider, input_tokens, output_tokens, latency_ms,
                 cost_usd_micro, metadata)
            VALUES ($1, 'query', $2, $3, $4, $5, $6, $7)
            """,
            tenant_id,
            result.provider,
            result.input_tokens,
            result.output_tokens,
            result.latency_ms,
            cost,
            json.dumps({"retrieved": len(hits)}),
        )
    return RAGResponse(
        answer=result.text,
        citations=citations,
        provider=result.provider,
        latency_ms=int((time.perf_counter() - start) * 1000),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        retrieved=len(hits),
    )
