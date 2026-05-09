# Design Trade-offs

This document covers (a) the choices behind the implementation, and (b) the
parts of the spec that are documented rather than built, with the reasoning,
the design we'd ship, and the cost. The goal is to make every "we'd do X"
falsifiable: numbers, components, and failure modes — not adjectives.

## TL;DR — what's built vs. what's documented

| Spec requirement                          | Built | Documented only |
|-------------------------------------------|:-----:|:---------------:|
| **Part 1 — Ingestion**                    |       |                 |
| Async ingestion pipeline                  | ✓     |                 |
| Chunking (recursive 800/100)              | ✓     |                 |
| Metadata extraction                       | ✓     | semantic / table extraction |
| Embedding generation (local BGE-small)    | ✓     | provider-failover for embeddings |
| Vector indexing (Qdrant, per tenant)      | ✓     |                 |
| Retry + DLQ                               | ✓     |                 |
| Idempotent processing (content hash)      | ✓     |                 |
| PDF / DOCX / plain text                   | ✓     | HTML, OCR-scanned PDFs |
| Versioned documents                       |       | ✓               |
| Incremental re-indexing                   |       | ✓               |
| **Part 2 — RAG**                          |       |                 |
| Semantic + BM25 hybrid + RRF              | ✓     |                 |
| Metadata filtering (document_ids)         | ✓     | rich payload filters |
| Citations (chunk-level)                   | ✓     | sentence-level spans |
| Multi-document synthesis                  | ✓     |                 |
| Re-ranker (Cohere / bge-reranker)         |       | ✓               |
| Query rewriting                           |       | ✓               |
| Conversation memory                       | (table) | ✓             |
| **Part 3 — Multi-tenant**                 |       |                 |
| Tenant-aware ingestion + retrieval        | ✓     |                 |
| Hard isolation (collection-per-tenant + RLS)| ✓   |                 |
| Tenant API keys                           | ✓     | RBAC roles      |
| Rate limiting per tenant                  | ✓     |                 |
| Per-tenant quotas (cols + read paths)     | ✓     | hard enforcement on ingest |
| Cost tracking (per call, per provider)    | ✓     | per-tenant invoicing rollups |
| **Part 4 — Reliability**                  |       |                 |
| LLM provider failover (Groq → Gemini)     | ✓     |                 |
| Retries with backoff                      | ✓     |                 |
| Backpressure (rate-limit + 429)           | ✓     | queue-depth-aware backpressure |
| Vector DB downtime tolerance              |       | ✓               |
| Multi-region                              |       | ✓               |

---

## 1. Chunking strategy: recursive 800 / 100 overlap

**Choice:** recursive character split with separators `["\n\n","\n",". "," ",""]`,
800 chars per chunk, 100-char overlap.

**Why these numbers:**
- BGE-small is trained on ≤512 token windows; 800 chars ≈ 200 tokens, well
  within the encoder's optimal range and small enough that ~5 chunks fit in a
  single LLM context without crowding.
- 100-char overlap (~12% of chunk) prevents context cliffs on sentence
  boundaries; bigger overlap doubles storage and embedding cost without
  measurable recall improvement on enterprise docs.

**What we'd add at scale:** a *semantic* chunker that splits on topic
boundaries. The simplest version: compute embeddings sentence-by-sentence,
split where cosine similarity to the running mean drops below a threshold.
Trade-off: ~3× embedding cost during ingestion for typically +3–5 points of
retrieval recall on long-form documents (legal contracts, RFPs).

## 2. Vector DB: collection-per-tenant

**Choice:** one Qdrant collection per tenant.

**Pros:**
- Hard isolation. A bug in the retrieval path can't leak vectors across
  tenants — there is no shared collection.
- Per-tenant indexing parameters (HNSW M, ef) are possible; useful when one
  tenant has 10M chunks and another has 10K.
- Tenant deletion is `DELETE COLLECTION`, fast and atomic.

**Cons:**
- Doesn't scale past O(1k) tenants on a single Qdrant cluster — collection
  metadata, replication, and snapshot operations have per-collection overhead.
- Cross-tenant analytics are impossible (acceptable here; arguably a feature).

**What we'd ship at 10k+ tenants:** *shard* tenants across multiple Qdrant
clusters (sticky by tenant_id hash), or migrate to a single collection with
mandatory `tenant_id` payload filter — the latter is cheaper but loses the
"impossible-by-construction" leak guarantee. We'd want a row in the platform
team's threat model document before making that call.

## 3. BM25 inside Postgres (no Elastic / OpenSearch)

**Choice:** Postgres `tsvector` + `ts_rank`.

**Why:** one less service to deploy and pay for, and `tsvector` indexes are
plenty fast at our chunk sizes — `ts_rank` over a GIN index returns the top-K
in single-digit ms for collections of millions of rows.

**Limit:** `ts_rank` is TF-based, not real BM25 (no IDF saturation, no length
normalization). For tenants where keyword recall actually matters
(legal/compliance), we'd switch to OpenSearch and use proper BM25 — Postgres
becomes the document/chunk metadata store only.

## 4. Hybrid merge: Reciprocal Rank Fusion

**Choice:** RRF with k=60.

**Why:** parameter-free, robust to score-scale differences (BM25 raw scores
and Qdrant cosine are not comparable), and well-documented in the IR
literature. We considered weighted linear combination but it requires
calibration per tenant — RRF doesn't.

**What we'd add:** a re-ranker (Cohere Rerank or `bge-reranker-base`) on the
top-30 fused results, then keep the top-8 for the LLM. Re-rankers consistently
add 5–15 nDCG points for the cost of a single cross-encoder forward pass per
candidate (~50ms for 30 candidates on CPU).

## 5. Tenant isolation: keys + RLS + per-tenant collections

Three layers, deliberately defense-in-depth:

1. **Auth layer.** Bearer API key → SHA-256 hash → tenants row → `Tenant`
   object stamped into every dependency.
2. **Database layer.** Every request runs inside a transaction with
   `SET LOCAL app.tenant_id = '<uuid>'`. Postgres RLS policies on `documents`,
   `chunks`, `usage_events` reject any row whose `tenant_id` doesn't match.
3. **Vector layer.** Each tenant has its own Qdrant collection
   (`t_<uuid_no_dashes>`); cross-tenant queries are not just filtered out —
   they cannot be expressed.

**Why three layers:** a bug at any one layer doesn't compromise isolation. The
auth layer can be bypassed by a route that forgets `Depends(require_tenant)`;
RLS catches it. RLS can be misconfigured; the Qdrant collection split catches
it. We've seen each of these go wrong independently in production systems.

## 6. Rate limiting in Postgres

**Choice:** token bucket persisted in `rate_buckets`, enforced via
`SELECT ... FOR UPDATE`.

**Why:** no extra service. Adds ~3–5 ms to each request at low concurrency,
which is negligible compared to the LLM call itself.

**Limit:** the row-level lock is a hot spot at >1k RPS per tenant. **At
scale we'd front this with Redis** (Upstash, free tier) using a single Lua
script per check. We left Redis out to keep the demo to two external
dependencies (Supabase + Qdrant).

## 7. LLM failover: Groq primary, Gemini fallback

**Choice:** ordered list of providers, each tried in turn with a 2-attempt
retry on transient errors. First success wins.

**Why this order:**
- **Groq** is the fastest on the market for OSS models (~300 tok/s on Llama
  3.3 70B). For a synthesis prompt with ~3k input tokens and ~400 output, we
  measure end-to-end response in ~1.5–2.5s.
- **Gemini 1.5 Flash** is slower (~150 tok/s), cheaper per token, and run
  by an entirely different provider — good failover diversity.

**What we'd add for prod:** circuit breaker (open after N consecutive failures
within a window) so we don't waste 1–2s on a known-down provider. The
abstraction supports it; the wiring is one `pybreaker` instance per provider.

## 8. Embeddings: local BGE-small, not a hosted provider

**Choice:** `BAAI/bge-small-en-v1.5` loaded once at startup, 384 dims.

**Why:**
- Free. The Voyage / OpenAI / Cohere free tiers run out fast at the spec's
  ingestion targets (50K docs/hr × ~10 chunks/doc = 500K embeddings/hr).
- Fast enough on a single Hugging Face Space CPU: ~200 chunks/sec on a
  shared-CPU machine.
- 130 MB model, ~400 MB resident — fits inside the free Space's 16 GB cap
  with plenty of headroom.

**What we'd add:** the same `embed_texts` interface gets a hosted backend
(Voyage `voyage-3-lite` is the current recall/cost sweet spot for English).
Provider failover for embeddings matters less than for LLMs because re-embed
on retry produces identical vectors — at-most-once is fine.

## 9. The 50K docs/hour scaling story

Spec target: **50K documents/hour, 5 MB average, 10M+ docs per tenant.**

Back-of-envelope:
```
50_000 docs / 3600 s = 14 docs/sec  ingestion rate
14 docs/sec × 5 MB = 70 MB/sec      object storage write throughput
14 docs/sec × ~10 chunks/doc = 140 chunks/sec embedding rate
```

A single CPU worker embeds at ~200 chunks/sec (BGE-small, batch=32). To absorb
peak bursts we'd want **3–5× headroom**, so:

| Component         | Steady state           | Burst-tolerant fleet                 |
|-------------------|------------------------|--------------------------------------|
| Ingest workers    | ~1 worker              | 4 workers (≥800 chunks/sec)          |
| Embedding         | 1 GPU shard or 8 CPU   | 4 GPU shards or 32 CPU               |
| Qdrant cluster    | 3-node, RF=2           | 6-node, RF=3 (failure domain spread) |
| Postgres          | db.r6g.xlarge          | db.r6g.2xlarge + read replicas       |
| Object storage    | S3 / R2                | S3 / R2 (no scaling needed)          |

Per tenant at 10M docs:
- 10M × 10 chunks × 384 dims × 4 bytes = **~150 GB raw vectors** (uncompressed)
- With Qdrant scalar quantization (int8) → ~40 GB — fits comfortably on a
  single shard up to ~4–5 such tenants per node.

**Cost ballpark** (steady-state, monthly, one large tenant @ 10M docs +
100K queries/day):
- Embedding (one-time backfill at $0 with local BGE)
- Qdrant Cloud: ~$200 / month for the shard housing this tenant
- Groq (3M queries × ~3.5K tokens) ≈ ~$20K / month at list price
- Postgres (Supabase pro): ~$25 / month

The LLM is by far the largest line item — this is why the cost-tracking and
rate-limiting columns are not a nice-to-have. A misconfigured tenant can run
up four-figure bills in hours.

## 10. Versioning + incremental re-indexing (documented)

**Design:** `documents` gains a `version` column; the unique key becomes
`(tenant_id, source_id, version)` instead of `(tenant_id, content_hash)`. On
re-upload of the same `source_id`, we (a) write a new `documents` row, (b)
re-chunk + re-embed, and (c) flip a `current_version_id` pointer atomically.
Old chunks remain queryable for a configurable retention period — important
for legal use cases that need point-in-time retrieval.

**Incremental re-indexing** uses chunk-level content hashes: only re-embed
chunks whose hash changed. For typical edits (adding a section to a 100-page
PDF) this is a 5–10× cost reduction over full re-embed.

## 11. HTML and OCR (documented)

**HTML:** `selectolax` (faster than BeautifulSoup) → strip nav/footer/script →
preserve heading hierarchy as chunk metadata for retrieval boost. Adds two
dependencies and a parser branch; about 100 lines.

**OCR-enabled scanned PDFs:** detect by counting extractable text per page; if
< some threshold, route to OCR. Two backends: **Tesseract** (free, lower
quality, runs locally) and **AWS Textract** (paid, higher quality, esp. for
forms/tables). The pipeline already has retry/DLQ for the OCR step's flakiness.

## 12. Conversation memory (documented)

Tables (`conversations`, `conversation_messages`) ship in the migration but
aren't wired into the query path. Design: `/v1/query` accepts an optional
`conversation_id`; on each call we rewrite the user's question with the last
N turns of context (single LLM call, ~$0.0005), then run retrieval against
the rewritten query. Memory is per-tenant, RLS-protected.

## 13. RBAC (documented)

`tenants → users → roles → documents` with three default roles
(`admin/member/viewer`) and per-document ACL overrides. The auth layer
becomes `(tenant_id, user_id, scopes)` instead of just `tenant_id`. RLS
policies grow a join through `document_acls` for read paths.

## 14. Things I would change with a second day

1. **Add the re-ranker.** Biggest single quality win; one new dependency
   (`bge-reranker-base`) and ~30 lines in `retrieval.py`.
2. **Move ingest payloads out of Postgres.** `ingest_jobs.payload BYTEA` is
   pragmatic for a demo — at 5 MB × 50K/hr it's 250 GB/hr of WAL. In prod we
   put the bytes in object storage and pass the URL through the queue.
3. **Switch the Postgres-as-queue to a real broker.** Postgres queues work
   fine to ~100 jobs/sec with `SKIP LOCKED`. Beyond that, use NATS JetStream
   or SQS — both have a Python async client and durable redrive policies.
4. **Wire OpenTelemetry traces.** I left this out for time; the structlog
   logs are tagged with `tenant_id` and `job_id` and we'd add an OTel
   exporter pointed at Honeycomb / Grafana Tempo in prod.
