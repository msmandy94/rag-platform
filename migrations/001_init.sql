-- Multi-tenant RAG platform schema
-- Single migration for the MVP. Designed for Supabase Postgres.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Tenants ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL UNIQUE,
    -- per-tenant config (model preferences live here for the design doc story)
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- quotas
    monthly_query_quota   INT NOT NULL DEFAULT 100000,
    monthly_ingest_quota  INT NOT NULL DEFAULT 1000000,
    rate_limit_query_rpm  INT NOT NULL DEFAULT 60,
    rate_limit_ingest_rpm INT NOT NULL DEFAULT 300,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Documents -------------------------------------------------------------
-- content_hash gives idempotent uploads; same hash for same tenant -> existing doc returned.
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | indexed | failed
    error           TEXT,
    chunk_count     INT NOT NULL DEFAULT 0,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, content_hash)
);
CREATE INDEX IF NOT EXISTS documents_tenant_status ON documents (tenant_id, status);

-- Chunks ----------------------------------------------------------------
-- Stores text + tsvector for BM25; vectors live in Qdrant (collection per tenant).
-- The Qdrant point ID equals chunks.id so we can join on retrieval.
CREATE TABLE IF NOT EXISTS chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    text            TEXT NOT NULL,
    -- tsvector for BM25-style hybrid retrieval (Postgres FTS, ts_rank uses TF/IDF-ish)
    text_tsv        TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS chunks_tenant_doc ON chunks (tenant_id, document_id);

-- Ingest job queue ------------------------------------------------------
-- Postgres-as-queue with SKIP LOCKED; payload holds bytes via storage path.
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- Inline bytes for the MVP (small docs). For prod we'd point at object storage.
    payload         BYTEA NOT NULL,
    attempts        INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    locked_until    TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_jobs_status_lock ON ingest_jobs (status, locked_until);

-- Dead letter queue -----------------------------------------------------
CREATE TABLE IF NOT EXISTS dlq (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    document_id     UUID,
    job_id          BIGINT,
    reason          TEXT NOT NULL,
    attempts        INT NOT NULL,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Usage events ----------------------------------------------------------
-- One row per query/ingest, used for cost tracking + per-tenant analytics.
CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,  -- query | ingest
    provider        TEXT,            -- groq | gemini | local-bge
    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    embed_chunks    INT NOT NULL DEFAULT 0,
    latency_ms      INT NOT NULL DEFAULT 0,
    cost_usd_micro  BIGINT NOT NULL DEFAULT 0,  -- micro-USD to avoid floats
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_events_tenant_time ON usage_events (tenant_id, created_at DESC);

-- Rate limit buckets ----------------------------------------------------
-- Simple token bucket persisted in Postgres. Per (tenant, action).
CREATE TABLE IF NOT EXISTS rate_buckets (
    tenant_id       UUID NOT NULL,
    action          TEXT NOT NULL,
    tokens          DOUBLE PRECISION NOT NULL,
    refilled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, action)
);

-- Conversations (mentioned for the design doc, used minimally) ----------
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,  -- user | assistant
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Row-level security ----------------------------------------------------
-- Application sets `app.tenant_id` via SET LOCAL on each transaction.
-- Service role bypasses RLS for the worker / admin paths.
ALTER TABLE documents             ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks                ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events          ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_messages ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_documents') THEN
        CREATE POLICY tenant_isolation_documents ON documents
            USING (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_chunks') THEN
        CREATE POLICY tenant_isolation_chunks ON chunks
            USING (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'tenant_isolation_usage') THEN
        CREATE POLICY tenant_isolation_usage ON usage_events
            USING (tenant_id::text = current_setting('app.tenant_id', true));
    END IF;
END $$;
