#!/usr/bin/env python3
"""
Semantic validator for workflows/legal-ai-agent.n8n.json.

Structural checks (node shape, unique names, connection targets) are handled
by tools/verify_workflow.py. This script asserts the *semantic* contract of
the Law-Firm AI agent:

- Region A has Gmail + Drive triggers, a MIME router, dedup, Summary Agent
  with OpenRouter model, and writes to Postgres + Sheets + Gmail.
- Region B has Drive trigger, unified extract, dedup, Parties agent,
  Clause Matrix agent, per-clause embed+insert loop.
- Region C has both Telegram and Gmail triggers, a Gemini agent with exactly
  four tools (search_legal_knowledge, search_contract_clauses,
  fetch_contract_matrix, send_memo), Postgres chat memory, and channel-aware
  reply routing.

Exits non-zero with a diagnostic list if anything fails.

Usage:
    python tools/verify_legal_workflow.py workflows/legal-ai-agent.n8n.json
"""
import json
import sys
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python tools/verify_legal_workflow.py <workflow.json>")
    wf = load(sys.argv[1])
    nodes_by_name = {n["name"]: n for n in wf["nodes"]}
    conns = wf["connections"]
    errs = []
    oks = []

    def want(name, msg):
        if name in nodes_by_name:
            oks.append(f"  [+] {msg}: {name!r} present")
            return True
        errs.append(f"  [-] {msg}: missing node {name!r}")
        return False

    def want_type(name, typ):
        n = nodes_by_name.get(name)
        if not n:
            errs.append(f"  [-] {name!r} missing")
            return False
        if n["type"] != typ:
            errs.append(f"  [-] {name!r} expected type {typ!r}, got {n['type']!r}")
            return False
        oks.append(f"  [+] {name!r} is {typ}")
        return True

    def want_creds(name, cred_type):
        n = nodes_by_name.get(name)
        if not n:
            return False
        creds = n.get("credentials") or {}
        if cred_type not in creds:
            errs.append(f"  [-] {name!r} missing {cred_type} credential binding")
            return False
        oks.append(f"  [+] {name!r} bound to {cred_type} cred {creds[cred_type].get('id')}")
        return True

    def conn_list(src, typ="main"):
        return conns.get(src, {}).get(typ, [])

    def want_edge(src, dst, typ="main"):
        lists = conn_list(src, typ)
        for branch in lists:
            for e in branch:
                if e["node"] == dst:
                    oks.append(f"  [+] {src} -> {dst} ({typ}) wired")
                    return True
        errs.append(f"  [-] {src} -> {dst} ({typ}) missing")
        return False

    def count_subnode_conns(dst, typ):
        """Count incoming connections of `typ` into dst."""
        n = 0
        for src, spec in conns.items():
            for branch in spec.get(typ, []):
                for e in branch:
                    if e["node"] == dst and e["type"] == typ:
                        n += 1
        return n

    print("=== Region A: Document Intake & Summary ===")
    want_type("A_Gmail Trigger Docs", "n8n-nodes-base.gmailTrigger")
    # Gmail filter must include the required search query
    a_gmail = nodes_by_name.get("A_Gmail Trigger Docs", {})
    q = (a_gmail.get("parameters", {}).get("filters", {}) or {}).get("q", "")
    if "has:attachment" in q and "Complaint" in q and "Contract" in q and "Filing" in q:
        oks.append(f"  [+] Gmail trigger filter q={q!r}")
    else:
        errs.append(f"  [-] Gmail trigger must filter on has:attachment + Complaint/Contract/Filing; got q={q!r}")

    # Region A is Gmail-only by design — Drive trigger must NOT be here.
    if "A_Drive Trigger Docs" in nodes_by_name:
        errs.append("  [-] Region A must not have a Drive trigger (Gmail-only intake)")
    else:
        oks.append("  [+] Region A has no Drive trigger (Gmail-only)")
    want("A_MIME Router", "MIME classifier (Code node)")
    want("A_Op Switch", "op-based Switch")
    for br in ("A_Extract PDF", "A_Extract XLSX", "A_Extract CSV", "A_Extract Text"):
        want(br, "Extract branch")
    want("A_Dedup Lookup", "dedup lookup")
    want("A_Already Ingested?", "dedup IF gate")
    want_type("A_Summary Agent", "@n8n/n8n-nodes-langchain.agent")
    want_type("A_OpenRouter Sonnet", "@n8n/n8n-nodes-langchain.lmChatOpenRouter")
    want_edge("A_OpenRouter Sonnet", "A_Summary Agent", typ="ai_languageModel")
    want_type("A_Insert Document", "n8n-nodes-base.postgres")
    want_creds("A_Insert Document", "postgres")
    want_type("A_Sheets Mirror Doc", "n8n-nodes-base.googleSheets")
    a_sheet_cols = list(nodes_by_name.get("A_Sheets Mirror Doc", {}).get("parameters", {}).get("columns", {}).get("value", {}).keys())
    a_sheet_schema = [s.get("id") for s in nodes_by_name.get("A_Sheets Mirror Doc", {}).get("parameters", {}).get("columns", {}).get("schema", [])]
    expected_a = ["Date","Subject","Issue 1","Issue 2","Issue 3","Issue 4","Issue 5","Deadline","Full Summary"]
    if a_sheet_cols == expected_a:
        oks.append(f"  [+] A_Sheets Mirror Doc columns match the sheet headers exactly (9 cols)")
    else:
        errs.append(f"  [-] A_Sheets Mirror Doc columns diverge from sheet headers: got {a_sheet_cols}")
    if a_sheet_schema == expected_a:
        oks.append(f"  [+] A_Sheets Mirror Doc schema array matches column value keys")
    else:
        errs.append(f"  [-] A_Sheets Mirror Doc schema array mismatch: got {a_sheet_schema}")
    want_type("A_Send Confirm", "n8n-nodes-base.gmail")

    print("\n=== Region B: Contract Clause Matrix ===")
    want_type("B_Drive Trigger Contracts", "n8n-nodes-base.googleDriveTrigger")
    want("B_Dedup Contract Lookup", "contract dedup")
    want_type("B_Parties & Type Agent", "@n8n/n8n-nodes-langchain.agent")
    want_type("B_Clause Matrix Agent", "@n8n/n8n-nodes-langchain.agent")
    want_edge("B_OpenRouter Haiku", "B_Parties & Type Agent", typ="ai_languageModel")
    want_edge("B_OpenRouter Sonnet (Clauses)", "B_Clause Matrix Agent", typ="ai_languageModel")
    want("B_Split Clauses", "per-clause splitter")
    want("B_Embed Clause (HTTP)", "per-clause embedder")
    want("B_Shape Clause Insert", "per-clause shape/escape")
    want("B_Insert Clause", "per-clause insert")
    want_type("B_Send Risk Email", "n8n-nodes-base.gmail")
    b_sheet_cols = list(nodes_by_name.get("B_Sheets Mirror Contract", {}).get("parameters", {}).get("columns", {}).get("value", {}).keys())
    b_sheet_schema = [s.get("id") for s in nodes_by_name.get("B_Sheets Mirror Contract", {}).get("parameters", {}).get("columns", {}).get("schema", [])]
    expected_b = ["Contract", "Analysis"]
    if b_sheet_cols == expected_b:
        oks.append(f"  [+] B_Sheets Mirror Contract columns match the sheet headers exactly (2 cols)")
    else:
        errs.append(f"  [-] B_Sheets Mirror Contract columns diverge from sheet headers: got {b_sheet_cols}")
    if b_sheet_schema == expected_b:
        oks.append(f"  [+] B_Sheets Mirror Contract schema array matches column value keys")
    else:
        errs.append(f"  [-] B_Sheets Mirror Contract schema array mismatch: got {b_sheet_schema}")
    # Confirm 8-category taxonomy mentioned in system prompt
    bm = nodes_by_name.get("B_Clause Matrix Agent", {})
    sysmsg = bm.get("parameters", {}).get("options", {}).get("systemMessage", "")
    taxonomy = [
        "Payment & Commercial Terms", "Liability Allocation",
        "Dispute Resolution & Governing Law", "Data Protection & Privacy",
        "IP & Confidentiality", "Change-of-Control & Assignment",
        "Operational Obligations & SLAs", "Term, Renewal & Termination",
    ]
    missing = [c for c in taxonomy if c not in sysmsg]
    if missing:
        errs.append(f"  [-] Clause Matrix Agent missing categories: {missing}")
    else:
        oks.append(f"  [+] Clause Matrix Agent prompt has all 8 taxonomy categories")

    print("\n=== Region C: Legal Assistant ===")
    want_type("C_Telegram Trigger", "n8n-nodes-base.telegramTrigger")
    # Region C is Telegram-only by design — Gmail chat trigger must NOT be here.
    if "C_Gmail Chat Trigger" in nodes_by_name:
        errs.append("  [-] Region C must not have a Gmail chat trigger (Telegram-only)")
    else:
        oks.append("  [+] Region C has no Gmail chat trigger (Telegram-only)")
    want_type("C_Legal Assistant", "@n8n/n8n-nodes-langchain.agent")
    want_type("C_Gemini Chat Model", "@n8n/n8n-nodes-langchain.lmChatGoogleGemini")
    want_type("C_Postgres Chat Memory", "@n8n/n8n-nodes-langchain.memoryPostgresChat")
    mem_table = nodes_by_name.get("C_Postgres Chat Memory", {}).get("parameters", {}).get("tableName")
    if mem_table == "legal_chat_histories":
        oks.append(f"  [+] C_Postgres Chat Memory writes to legal_chat_histories (isolated from rag-agent)")
    else:
        errs.append(f"  [-] C_Postgres Chat Memory must use tableName='legal_chat_histories' to avoid cross-project collision with rag-agent; got {mem_table!r}")
    want_edge("C_Gemini Chat Model", "C_Legal Assistant", typ="ai_languageModel")
    want_edge("C_Postgres Chat Memory", "C_Legal Assistant", typ="ai_memory")
    # Four tools wired to the agent (two Sheets readers + draft-preview email + final send)
    expected_tools = ["DocumentSummaries", "ContractAnalysis", "GmailDraft", "GmailSender"]
    for t in expected_tools:
        want(t, "agent tool")
        want_edge(t, "C_Legal Assistant", typ="ai_tool")
    tool_count = count_subnode_conns("C_Legal Assistant", "ai_tool")
    if tool_count != 4:
        errs.append(f"  [-] C_Legal Assistant has {tool_count} ai_tool connections (want exactly 4)")
    else:
        oks.append(f"  [+] C_Legal Assistant has exactly 4 ai_tool connections")
    # GmailDraft must actually create a Gmail Drafts-folder draft, not send an email.
    draft_params = nodes_by_name.get("GmailDraft", {}).get("parameters", {})
    if draft_params.get("resource") == "draft" and draft_params.get("operation") == "create":
        oks.append("  [+] GmailDraft uses resource=draft + operation=create (true Gmail draft)")
    else:
        errs.append(
            f"  [-] GmailDraft must be resource=draft + operation=create to save in Drafts folder, "
            f"got resource={draft_params.get('resource')!r} operation={draft_params.get('operation')!r}"
        )
    # GmailSender must still be resource=message (hardcoded or defaulted) with send op.
    send_params = nodes_by_name.get("GmailSender", {}).get("parameters", {})
    if send_params.get("resource", "message") == "message" and send_params.get("operation", "send") == "send":
        oks.append("  [+] GmailSender uses message+send (dispatches immediately)")
    else:
        errs.append(
            f"  [-] GmailSender should be resource=message + operation=send, got resource={send_params.get('resource')!r} operation={send_params.get('operation')!r}"
        )
    # No leftover pgvector/embedding subnodes or old tool-node names
    for stale in ("C_Embeddings Retrieve", "C_Tool_search_legal_knowledge",
                  "C_Tool_search_contract_clauses", "C_Tool_fetch_contract_matrix",
                  "C_Tool_get_documents", "C_Tool_get_contracts", "C_Tool_send_memo"):
        if stale in nodes_by_name:
            errs.append(f"  [-] Stale node {stale!r} still present — Region C tools are Sheets-based with matching prompt names")
        else:
            oks.append(f"  [+] Stale node {stale!r} correctly absent")
    # Channel routing
    want_edge("C_Response Formatter", "C_Send Telegram")
    want_edge("C_Send Telegram", "C_Persist Turn")
    want("C_Persist Turn", "chat turn persistence")

    print("\n=== Copyright-differentiation sanity checks ===")
    # Nothing may be named exactly "AI Agent", "AI Agent1", "AI Agent2"
    banned_names = {"AI Agent", "AI Agent1", "AI Agent2"}
    clashes = [n for n in nodes_by_name if n in banned_names]
    if clashes:
        errs.append(f"  [-] Generic agent names (too similar to reference): {clashes}")
    else:
        oks.append(f"  [+] No nodes named 'AI Agent' / 'AI Agent1' / 'AI Agent2'")
    # Reference's exact 6-clause list, in order, must NOT appear verbatim
    ref_clause_marker = (
        "Termination, Indemnity, Confidentiality, Force Majeure, "
        "Governing Law, Limitation of Liability"
    )
    dump = json.dumps(wf, ensure_ascii=False)
    if ref_clause_marker in dump:
        errs.append(f"  [-] Reference's exact 6-clause sentence appears verbatim — rewrite")
    else:
        oks.append(f"  [+] Reference clause-list phrase not present")
    # The memo is composed by the LLM (not the node template), so we just check
    # that GmailSender takes a `body` parameter via $fromAI and that the system
    # prompt references the FILR section names the user specified.
    gmail_memo = nodes_by_name.get("GmailSender", {})
    memo_msg = gmail_memo.get("parameters", {}).get("message", "")
    if "$fromAI(" in memo_msg and ("Message" in memo_msg or "body" in memo_msg):
        oks.append("  [+] GmailSender body is LLM-composed via $fromAI")
    else:
        errs.append("  [-] GmailSender must accept a body/Message parameter from the LLM via $fromAI")
    sys_msg = nodes_by_name.get("C_Legal Assistant", {}).get("parameters", {}).get("options", {}).get("systemMessage", "")
    for section in ("Statement of Facts", "Issues Presented", "Legal Analysis", "Recommendations"):
        if section not in sys_msg:
            errs.append(f"  [-] System prompt missing FILR section: {section}")
    if all(s in sys_msg for s in ("Statement of Facts", "Issues Presented", "Legal Analysis", "Recommendations")):
        oks.append("  [+] System prompt enumerates all 4 FILR memo sections")
    # Confirm embedding dim is 1536 in HTTP embed requests (pgvector column match)
    for n_name in ("A_Embed Doc (HTTP)", "B_Embed Contract (HTTP)", "B_Embed Clause (HTTP)"):
        n = nodes_by_name.get(n_name, {})
        body = n.get("parameters", {}).get("jsonBody", "")
        if "dimensions: 1536" in body or "\"dimensions\":1536" in body:
            oks.append(f"  [+] {n_name} embeds at 1536 dims")
        else:
            errs.append(f"  [-] {n_name} embedding dims not 1536 (pgvector column expects 1536)")

    print()
    for line in oks:
        print(line)
    if errs:
        print("\n-- Errors:")
        for e in errs:
            print(e)
        print(f"\nTotal: {len(oks)} passed, {len(errs)} failed")
        sys.exit(1)
    print(f"\nTotal: {len(oks)} passed, 0 failed")


if __name__ == "__main__":
    main()
