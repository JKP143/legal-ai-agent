# legal-ai-agent — AI Systems for Law Firms

Single n8n workflow (id `REPLACE_ME_WORKFLOW_ID`, name "AI Systems Law Firm") that delivers three legal-AI capabilities in one graph:

- **Region A** — Document Intake & Summary (Gmail only → Postgres + Sheets + team email)
- **Region B** — Contract Clause Matrix (Drive → 8-category clause breakdown + risk alert)
- **Region C** — Legal Assistant chatbot (Telegram, RAG-backed, sends IRAC memos)

## Objective

Turn inbound legal paperwork (complaints, contracts, filings) and interactive questions into structured, searchable, audited artefacts. Postgres (Neon, with pgvector) is the source of truth; Google Sheets is a human-readable mirror.

## Inputs

- Emails hitting the firm's Gmail matching `has:attachment (Complaint OR Contract OR Filing)`, unread only (Region A intake)
- Files dropped into the Drive folder `<LEGAL_DRIVE_CONTRACTS_FOLDER_ID>` — contracts only, PDF preferred; DOCX should be converted upstream (Region B intake)
- Telegram messages to the bound bot (any chat the bot is in)

## Outputs

- Rows in Postgres: `legal_documents`, `legal_contracts`, `legal_clauses`, `legal_chat_sessions`, `legal_chat_messages`, `legal_errors`
- Rows appended in two Google Sheets: `Documents` tab, `Contracts` tab
- Confirmation / risk-alert emails to `$LEGAL_TEAM_EMAIL`
- Chatbot replies on the same channel the question came from (Telegram HTML or in-thread Gmail HTML)

## Topology

```
REGION A  (y=0)
  Gmail Trigger (q filter) ─► Normalize ─► MIME Router ─► Extract Text ─► Hash & Wordcount ─► Dedup Lookup ─► Already Ingested? ─false─► Size Guard ─► Summary Agent (OpenRouter Claude Sonnet 4) ─► Parse JSON ─► Embed (OpenAI 1536d) ─► Shape Row ─► Insert legal_documents ─► Sheets Mirror ─► Build Confirm Email ─► Send Confirm
                                                                                                  ^-true-► (stop; dedup skip)

REGION B  (y=800)
  Drive Trigger (contracts) ─► Download ─► Extract PDF ─► Hash ─► Dedup ─► Already Ingested? ─false─► Parties Agent (Haiku) ─► Parse Parties ─► Clause Matrix Agent (Sonnet) ─► Parse Clauses JSON ─► Embed Contract ─► Shape Contract Insert ─► Insert legal_contracts ─► Split Clauses ─► (for each) Embed Clause ─► Shape Clause Insert ─► Insert legal_clauses ─► Aggregate ─► Sheets Mirror ─► Build Risk Email ─► Send Risk Email

REGION C  (y=1600)
  Telegram Trigger ─► Channel Normalize ─► Session Upsert ─► Merge Session ─► Legal Assistant (Gemini 2.5 Pro)
                                                                                  │  tools: search_legal_knowledge · search_contract_clauses · fetch_contract_matrix · send_memo
                                                                                  │  memory: Postgres Chat Memory on legal_chat_histories (session_key = legal:telegram:<chat_id>)
                                                                                  ▼
                                                                     Response Formatter ─► Send Telegram ─► Persist Turn
```

## Prerequisites

### 1. Database schema

Apply `sql/010_legal_ai.sql` against the Neon database. It's idempotent (`CREATE TABLE IF NOT EXISTS`) and uses the existing `vector` extension from `sql/000_pgvector_and_documents.sql`.

```bash
psql "$LEGAL_RW_PG_DSN" -f sql/010_legal_ai.sql
```

Optional (for the chatbot's RAG tools to use a least-privilege principal):

```sql
CREATE ROLE legal_ro LOGIN PASSWORD '<strong-password>';
ALTER ROLE legal_ro SET statement_timeout = '5s';
GRANT USAGE ON SCHEMA public TO legal_ro;
GRANT SELECT ON legal_documents, legal_contracts, legal_clauses,
                legal_chat_sessions, legal_chat_messages TO legal_ro;
```

Then create a second Postgres credential in n8n pointing at `legal_ro` and swap it in on the three tool nodes (`C_Tool_search_legal_knowledge`, `C_Tool_search_contract_clauses`, `C_Tool_fetch_contract_matrix`). For v1 the workflow reuses the existing `NeonDB` credential (writer); swap later for production.

### 2. Google Workspace

- Create the Drive folder for contracts and note its ID → `LEGAL_DRIVE_CONTRACTS_FOLDER_ID` in `.env` (Region A no longer watches Drive — only Region B does)
- No Gmail label is needed (Region C is Telegram-only)
- Create two Google Sheets (one for the Documents mirror, one for Contracts) and note their IDs → `LEGAL_SHEET_DOCS_ID`, `LEGAL_SHEET_CONTRACTS_ID`
- In the Documents sheet add a tab named `Documents` with headers: `doc_id, received_at, source, title, risk_score, suggested_routing, executive_brief, action_items, deadlines`
- In the Contracts sheet add a tab named `Contracts` with headers: `contract_id, title, contract_type, parties, effective_date, overall_risk, clause_count, risk_summary`
- In Gmail, create a label `legal-chat/inbound` — note its ID via `GET /gmail/v1/users/me/labels` → `LEGAL_GMAIL_CHAT_LABEL_ID`

### 3. Credentials in n8n (all already registered on localhost:5678)

| Purpose | Credential name | Credential id |
|---|---|---|
| Gmail (intake + chat + sends) | AI account | `REPLACE_ME_GMAILOAUTH2_CREDENTIAL_ID` |
| Google Drive (triggers + download) | Google Drive account | `REPLACE_ME_GOOGLEDRIVEOAUTH2API_CREDENTIAL_ID` |
| Google Sheets (mirror tabs) | Google Sheets account | `REPLACE_ME_GOOGLESHEETSOAUTH2API_CREDENTIAL_ID` |
| OpenRouter (Claude Sonnet + Haiku) | OpenRouter account | `REPLACE_ME_OPENROUTERAPI_CREDENTIAL_ID` |
| Google Gemini (chat model) | Google Gemini(PaLM) Api account | `REPLACE_ME_GOOGLEPALMAPI_CREDENTIAL_ID` |
| OpenAI (embeddings) | OpenAI account | `REPLACE_ME_OPENAIAPI_CREDENTIAL_ID` |
| Telegram (chatbot) | RAG AI Agent (reused) | `REPLACE_ME_TELEGRAMAPI_CREDENTIAL_ID` |
| Postgres | NeonDB | `REPLACE_ME_POSTGRES_CREDENTIAL_ID` |

If you want a dedicated Law-Firm Telegram bot, create a new credential in the UI and rebind `C_Telegram Trigger` + `C_Send Telegram`.

## Build / Sync procedure

1. Edit `.tmp/build_legal_workflow.py` if you want to tweak node topology or prompts
2. Regenerate the JSON: `python .tmp/build_legal_workflow.py` (overwrites `workflows/legal-ai-agent.n8n.json`)
3. Structural validate: `python tools/verify_workflow.py workflows/legal-ai-agent.n8n.json`
4. Semantic validate: `python tools/verify_legal_workflow.py workflows/legal-ai-agent.n8n.json`
5. Sync to live n8n: `python tools/sync_workflow.py workflows/legal-ai-agent.n8n.json` (auto-resolves the workflow id from `N8N_LEGAL_AI_WORKFLOW_ID`)
6. Open the canvas in the browser — verify credentials are bound on every node (the UI shows red dots on any that aren't)
7. Activate the workflow

## Verification (end-to-end smoke tests)

### Region A
- Send an email with `Contract` in the subject and a PDF attachment to the Gmail account from an external address.
- Within ~3 minutes, `SELECT id, title, risk_score FROM legal_documents ORDER BY id DESC LIMIT 1;` should show a row.
- The Documents Sheet should have one new row.
- Your team inbox should receive the branded "New document ingested" email.
- Re-send the *same* attachment — the dedup gate should skip it (no new row, no new email).

### Region B
- Drop a sample NDA or MSA PDF into the Contracts folder on Drive.
- `SELECT id, title, overall_risk_score FROM legal_contracts ORDER BY id DESC LIMIT 1;` should show the parent row.
- `SELECT category, risk_level FROM legal_clauses WHERE contract_id = <id>;` should show one row per detected clause.
- The Contracts sheet gains a summary row; your inbox receives the "Contract analysed" risk-matrix email with coloured badges.

### Region C — Telegram
- `@your-bot`: `"What are the last three contracts I uploaded, and the highest-risk clauses in each?"`
- Expect the agent to call `search_contract_clauses` and/or `fetch_contract_matrix`, return a cited summary. `[contract #N]` references should match real ids.

### Region C — Gmail
- Send an email to the account with label `legal-chat/inbound` applied: `"Draft a memo on termination risk in contract #5."`
- Expect an in-thread reply within ~3 minutes. If the agent called `send_memo`, you'll also get a second email with the full IRAC memo.

### Dedup + error paths
- Upload the same document twice → only one DB row, only one confirmation email (the `ON CONFLICT (body_hash) DO NOTHING` plus dedup IF gate both protect).
- Temporarily revoke OpenRouter credential → execution should fail cleanly on the Agent node; nothing should be inserted.

## Operations & gotchas

- **Postgres `queryReplacement` can't carry embeddings.** n8n's `queryReplacement` splits the expression on commas, which destroys 1536-float embedding arrays. Every INSERT here uses a preceding Code node to escape single quotes into `safe_*` fields, then inlines them into the query via `{{ }}` expressions (same pattern as `rag-agent`'s `Delete Metadata (Trashed)` node). When you add new Postgres writes, follow this pattern — don't try to pass `$1, $2, $3...` with multiple params.
- **Embedding dimension must stay 1536.** The `legal_documents.embedding`, `legal_contracts.embedding`, `legal_clauses.embedding` columns are `vector(1536)` to match `sql/000_pgvector_and_documents.sql`. All three HTTP embed nodes explicitly set `dimensions: 1536`. Changing the embedding model requires an `ALTER TABLE ... ALTER COLUMN embedding TYPE vector(<new>)` with a re-embed.
- **Gmail polling is unread-only + mark-as-read.** Without `markAsRead: true`, every poll would re-ingest every matching email. The filter `q` narrows to `has:attachment (Complaint OR Contract OR Filing)` to avoid firing on unrelated threads. If you change the filter, also add the search string to `tools/verify_legal_workflow.py` so the semantic check stays useful.
- **Drive trigger fires on `fileCreated` only.** If you also want "file replaced" events, add a second Drive Trigger with `event: fileUpdated` feeding the same downstream path.
- **Sheets mirror requires existing tabs.** If the `Documents` / `Contracts` tabs don't exist, the Sheets append errors out with a confusing "range not found" — always pre-create the tab and its header row.
- **Postgres chat memory survives restarts.** Session key is `legal:<channel>:<session_key>`; Telegram uses `chat.id`, Gmail uses `threadId`. If a user messages from both channels they get separate histories by design — cross-channel linking is out of scope for v1.
- **Memory table is `legal_chat_histories`, NOT the default `n8n_chat_histories`.** The RAG agent (`workflows/rag-agent.n8n.json`) already writes to `n8n_chat_histories`; if this workflow used the default too, the two bots would cross-contaminate each other's conversation memory. Any new langchain chat-memory subnode in this project must set `tableName` explicitly.
- **IRAC memo is the `send_memo` template.** The reference YouTuber workflow uses FILR (Facts / Issues / Legal / Recommendations); this workflow uses IRAC (Issue / Rule / Application / Conclusion) both to be substantively different and because IRAC is the more common law-school standard.
- **Clause taxonomy is fixed at 8 categories.** The list is inline in the `B_Clause Matrix Agent` system prompt. Changes require regeneration (`python .tmp/build_legal_workflow.py`) + sync. Sheet mirror column `clause_count` is a simple length of the returned array — no validation that categories match the prompt list.
- **The two Gmail triggers share one credential.** If the chat trigger starts polling the intake label, its `q` filter won't stop it — the `labelIds` filter is authoritative on `C_Gmail Chat Trigger`. Keep labels disjoint.
- **`availableInMCP` re-toggles.** Per `reference_n8n_availableinmcp_sync.md`, `sync_workflow.py` PUTs via the public REST API which strips MCP settings. If you rely on MCP access, re-toggle in the UI after each sync, or extend `sync_workflow.py` to PATCH `/rest/mcp/workflows/{id}/toggle-access` via the session cookie.

## Out of scope (v1)

- Document chunking for >25 k-word files (current behaviour: truncate to 25 k with a marker)
- Error-funnel table + Telegram alert (schema provisions `legal_errors` but no nodes write to it yet — add an Error Trigger sub-workflow later)
- Cost tracking to a dashboard (token columns exist in the DB; no read-side aggregation)
- Separate `legal_ro` Postgres credential (v1 reuses writer; swap in production)
- DOCX extraction (PDF-only for Region B; convert DOCX → PDF upstream)

## Self-improvement loop

1. `python tools/list_executions.py --workflow REPLACE_ME_WORKFLOW_ID` — recent runs
2. `python tools/fetch_execution.py <id>` — see the actual data per node on failure
3. Fix the offending node or Code logic in `.tmp/build_legal_workflow.py`
4. Regenerate → re-verify → re-sync
5. Update this SOP's "Operations & gotchas" section with anything learned so the lesson isn't paid twice
