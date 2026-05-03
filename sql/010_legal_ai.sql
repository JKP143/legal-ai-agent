-- ============================================================================
-- 010_legal_ai.sql — Schema for the AI Systems for Law Firms workflow
-- Target: Neon Postgres (assumes sql/000_pgvector_and_documents.sql already ran,
-- so the `vector` extension is enabled).
--
-- Tables isolated under a `legal_*` prefix to avoid collision with the existing
-- RAG tables (`documents`, `document_metadata`, `document_rows`).
--
-- Apply with:
--   psql "$LEGAL_RW_PG_DSN" -f sql/010_legal_ai.sql
--
-- Rollback (destructive): see bottom of file.
-- ============================================================================

BEGIN;

-- Belt-and-suspenders: pgvector may or may not already be enabled.
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- legal_documents — outputs of Region A (Document Intake & Summary)
-- One row per ingested document (email attachment OR Drive file).
-- body_hash enables content-based dedup across both intake channels.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legal_documents (
    id               bigserial PRIMARY KEY,
    source           text        NOT NULL CHECK (source IN ('gmail','drive')),
    source_ref       text        NOT NULL,                 -- gmail msg id or drive file id
    title            text,
    mime_type        text,
    body_text        text        NOT NULL,
    body_hash        char(64)    NOT NULL UNIQUE,          -- sha256 hex; UNIQUE enforces dedup
    executive_brief  text,
    key_facts        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    action_items     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    deadlines        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    risk_score       smallint    CHECK (risk_score BETWEEN 0 AND 10),
    suggested_routing text,
    embedding        vector(1536),
    execution_id     text,
    model_used       text,
    tokens_in        integer,
    tokens_out       integer,
    cost_usd         numeric(10,6),
    received_at      timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS legal_documents_embedding_idx
    ON legal_documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS legal_documents_source_ref_idx ON legal_documents (source, source_ref);
CREATE INDEX IF NOT EXISTS legal_documents_received_at_idx ON legal_documents (received_at DESC);

-- ---------------------------------------------------------------------------
-- legal_contracts — outputs of Region B (Contract Clause Matrix)
-- Parent row; each clause lives in legal_clauses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legal_contracts (
    id                   bigserial PRIMARY KEY,
    source_ref           text        NOT NULL,             -- drive file id
    title                text,
    contract_type        text,                              -- e.g. NDA, MSA, SOW
    parties              jsonb       NOT NULL DEFAULT '[]'::jsonb,
    effective_date       date,
    body_hash            char(64)    NOT NULL UNIQUE,
    overall_risk_score   smallint    CHECK (overall_risk_score BETWEEN 0 AND 10),
    risk_summary         text,
    embedding            vector(1536),                       -- embedding of the whole contract
    execution_id         text,
    model_used           text,
    tokens_in            integer,
    tokens_out           integer,
    cost_usd             numeric(10,6),
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS legal_contracts_embedding_idx
    ON legal_contracts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS legal_contracts_source_ref_idx ON legal_contracts (source_ref);

-- ---------------------------------------------------------------------------
-- legal_clauses — one row per extracted clause.
-- Stored with per-clause embeddings so the chatbot can cite specific clauses
-- (not just "the NDA"). category values are from the 8-category taxonomy
-- defined in workflows/legal-ai-agent.md.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legal_clauses (
    id            bigserial PRIMARY KEY,
    contract_id   bigint      NOT NULL REFERENCES legal_contracts(id) ON DELETE CASCADE,
    category      text        NOT NULL,
    clause_text   text        NOT NULL,
    page_ref      text,
    risk_level    text        CHECK (risk_level IN ('low','medium','high')),
    rationale     text,
    embedding     vector(1536),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS legal_clauses_contract_idx ON legal_clauses (contract_id);
CREATE INDEX IF NOT EXISTS legal_clauses_category_idx ON legal_clauses (category);
CREATE INDEX IF NOT EXISTS legal_clauses_embedding_idx
    ON legal_clauses USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- legal_chat_sessions + legal_chat_messages — chatbot memory, channel-agnostic.
-- Survives n8n restarts and lets Telegram + Gmail threads share history if
-- the user is mapped across both channels later.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legal_chat_sessions (
    id              bigserial PRIMARY KEY,
    channel         text        NOT NULL CHECK (channel IN ('telegram','gmail')),
    session_key     text        NOT NULL,                  -- telegram chat_id or gmail thread_id
    user_display    text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, session_key)
);

CREATE TABLE IF NOT EXISTS legal_chat_messages (
    id          bigserial PRIMARY KEY,
    session_id  bigint      NOT NULL REFERENCES legal_chat_sessions(id) ON DELETE CASCADE,
    role        text        NOT NULL CHECK (role IN ('user','assistant','tool')),
    content     text        NOT NULL,
    tool_name   text,
    tokens      integer,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS legal_chat_messages_session_idx
    ON legal_chat_messages (session_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- legal_errors — central error funnel. Populated by the Error Funnel Code node
-- from every Region's onError branch. Dedupe key = (execution_id, node_name)
-- so a single execution doesn't double-log.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS legal_errors (
    id            bigserial PRIMARY KEY,
    execution_id  text        NOT NULL,
    node_name     text        NOT NULL,
    region        text,                                    -- 'A' | 'B' | 'C'
    severity      text        NOT NULL DEFAULT 'error' CHECK (severity IN ('warn','error','critical')),
    message       text,
    payload       jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (execution_id, node_name)
);

CREATE INDEX IF NOT EXISTS legal_errors_created_idx ON legal_errors (created_at DESC);

-- ---------------------------------------------------------------------------
-- Retrieval views — expose legal_documents / legal_clauses under the column
-- names langchain's PGVector integration expects (content text, metadata jsonb,
-- embedding vector). The workflow's vector-store tools point at these views,
-- not the base tables, so the tool nodes can use default column config.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW legal_documents_for_retrieval AS
SELECT
    id,
    executive_brief AS content,
    jsonb_build_object(
        'doc_id',      id,
        'title',       title,
        'risk_score',  risk_score,
        'routing',     suggested_routing,
        'received_at', received_at,
        'key_facts',   key_facts,
        'deadlines',   deadlines
    ) AS metadata,
    embedding
FROM legal_documents
WHERE embedding IS NOT NULL;

CREATE OR REPLACE VIEW legal_clauses_for_retrieval AS
SELECT
    cl.id,
    cl.clause_text AS content,
    jsonb_build_object(
        'clause_id',    cl.id,
        'contract_id',  cl.contract_id,
        'category',     cl.category,
        'risk_level',   cl.risk_level,
        'rationale',    cl.rationale,
        'page_ref',     cl.page_ref,
        'contract_title', c.title
    ) AS metadata,
    cl.embedding
FROM legal_clauses cl
LEFT JOIN legal_contracts c ON c.id = cl.contract_id
WHERE cl.embedding IS NOT NULL;

COMMIT;

-- ============================================================================
-- Role grants — run AFTER creating the legal_ro login role.
-- Create the role once (outside this transaction):
--
--   CREATE ROLE legal_ro LOGIN PASSWORD '<paste-strong-password>';
--   ALTER ROLE legal_ro SET statement_timeout = '5s';
--
-- Then run the GRANT block below. We do NOT create the role here because Neon
-- ownership rules differ across branches; mirror the pattern from
-- sql/003_rag_readonly_role.sql.
-- ============================================================================

-- GRANT USAGE ON SCHEMA public TO legal_ro;
-- GRANT SELECT ON legal_documents, legal_contracts, legal_clauses,
--                legal_chat_sessions, legal_chat_messages TO legal_ro;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public
--   GRANT SELECT ON TABLES TO legal_ro;

-- Safety checks (run manually to verify):
--   SELECT has_table_privilege('legal_ro','legal_documents','SELECT');   -- expect t
--   SELECT has_table_privilege('legal_ro','legal_documents','INSERT');   -- expect f
--   SELECT has_table_privilege('legal_ro','legal_errors','SELECT');      -- expect t (read-only audit)

-- ============================================================================
-- Rollback (destructive — only run if you're sure):
-- ============================================================================
-- BEGIN;
--   DROP TABLE IF EXISTS legal_chat_messages;
--   DROP TABLE IF EXISTS legal_chat_sessions;
--   DROP TABLE IF EXISTS legal_clauses;
--   DROP TABLE IF EXISTS legal_contracts;
--   DROP TABLE IF EXISTS legal_documents;
--   DROP TABLE IF EXISTS legal_errors;
-- COMMIT;
