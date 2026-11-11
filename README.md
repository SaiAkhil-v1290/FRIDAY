# FRIDAY — Phase 1: Architecture Stabilization & Security Hardening

A local-first Windows AI assistant. FRIDAY understands natural-language requests,
converts them into validated structured plans, enforces a formal approval workflow
for sensitive actions, and executes only after deterministic policy validation.

---

## Requirements

- Windows 10 / 11
- Python 3.12 (recommended)
- [Ollama for Windows](https://ollama.com/download/windows)
- A model already pulled in Ollama (default: `qwen2.5:3b`)

---

## Setup (PowerShell)

```powershell
py -3.12 -m venv .venv

# If script execution is blocked in this session:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
ollama pull qwen2.5:3b
```

Make sure Ollama is running (the Windows app normally keeps it in the background).
If needed, start the server manually in a separate terminal:

```powershell
ollama serve
```

Then start FRIDAY:

```powershell
python main.py
```

---

## Example requests

```
Open Chrome and search for local-first AI agents
Find my resume in Downloads
Draft an email asking for an assignment extension
Search my documents for the word "invoice"
Close Notepad
```

---

## Approval workflow

For sensitive or high-risk actions, FRIDAY proposes a plan and waits for explicit
approval before executing. The terminal displays:

```
Pending approval — run 'approve <id>' to execute or 'reject <id>' to cancel.
```

Type the full command at the FRIDAY prompt:

```
approve 3f2a1b4c-...     # approve and execute the pending action
reject  3f2a1b4c-...     # cancel the action
```

**Approval guarantees:**
- Each approval ID is unique (UUID4) and single-use.
- Approvals are time-limited (default: 300 seconds).
- An expired approval cannot be executed.
- A rejected approval cannot be re-approved.
- An executed approval cannot be replayed.
- Approval is bound to the exact tool name and validated arguments; altering
  either invalidates the token.
- The LLM cannot approve its own actions — approval is a deterministic Python operation.

---

## Permission enforcement

Every tool has an explicit permission classification:

| Risk level | Examples | Requires approval |
|------------|----------|-------------------|
| `low` | Check if an application is running, browser page info | No |
| `medium` | Search files, search document contents | No |
| `high` | Open application, browser navigation/search | No (auto-approved in plan) |
| `critical` | Delete files, send email, close application | **Yes — explicit approval required** |

Unknown or unregistered tools are **denied by default**. Argument validation
runs at execution time (not only at plan time), so an altered plan cannot bypass
policy.

---

## File access boundaries

File and document operations are restricted to these approved locations:

- Desktop
- Documents
- Downloads

Path traversal, symlink attacks, and absolute paths outside these roots are
blocked. The safe-root check is re-applied immediately before every file open,
reveal, or folder open call (TOCTOU mitigation).

### Document extraction limits

| Limit | Default |
|-------|---------|
| Maximum file size | 10 MB |
| Maximum extracted characters | 500,000 |
| Maximum extracted units (paragraphs / slides / rows) | 5,000 |

Files exceeding the size limit are skipped rather than partially extracted.

---

## Process termination safeguards

- Only applications on the registered allowlist can be terminated.
- Applications marked `closable: false` (e.g., system-critical processes) are
  protected from termination.
- Alias resolution prevents bypassing the allowlist via alternate process names.
- System PIDs (0, 4 on Windows) are protected by OS-level `PermissionError`.

---

## Configuration

Copy `.env.example` to `.env` and edit as needed:

```env
# Ollama connection
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_EMBEDDING_MODEL=embeddinggemma
OLLAMA_TIMEOUT=120
ALLOW_REMOTE_OLLAMA=false

# Conversation
MAX_HISTORY_MESSAGES=12

# Phase 1 security settings (optional — these are the defaults)
MAX_DOCUMENT_SIZE_BYTES=10485760
MAX_EXTRACTED_CHARS_PER_DOC=500000
MAX_EXTRACTION_UNITS_PER_DOC=5000
APPROVAL_TIMEOUT_SECONDS=300
```

**Configuration validation:** All values are validated at startup. Invalid
URLs, non-numeric numbers, or out-of-range values produce a clear error and
halt startup. Secrets are never echoed in logs or error messages.

Ollama hosts must be loopback by default. Setting `ALLOW_REMOTE_OLLAMA=true`
explicitly permits a non-loopback endpoint. FRIDAY then sends prompts and any
retrieved private-memory text to that configured host. Use only a remote host
you control and trust; the application cannot determine whether a remote host
is a public cloud service.

---

## Running the tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

Verified on 2026-11-11: see `reports/PHASE_2_IMPLEMENTATION_REPORT.md` for the latest full-suite result.

---

## Security boundaries

| Boundary | Status |
|----------|--------|
| No arbitrary shell execution | ✅ Enforced |
| No arbitrary Python eval / exec | ✅ Enforced |
| No screen capture | ✅ Not implemented |
| File access outside approved roots blocked | ✅ Enforced |
| Symlink / path-traversal prevention | ✅ Enforced |
| Pydantic schema validation on all tool arguments | ✅ Enforced |
| Per-tool argument schemas with `extra="forbid"` | ✅ Enforced |
| Formal approval workflow for high-risk actions | ✅ Enforced |
| Approval tokens are single-use and time-bounded | ✅ Enforced |
| LLM cannot approve its own actions | ✅ Enforced |
| Audit log with automatic secret redaction | ✅ Enforced |
| Structured error responses (never silent swallow) | ✅ Enforced |

---

## Current limitations

- Working memory and approval requests exist only in RAM and are discarded on exit.
- Audit log is in-memory only; not persisted across restarts.
- TOCTOU re-validation at file-open time reduces but cannot fully eliminate race
  conditions on Windows (no atomic open-with-containment OS primitive).
- PID reuse is possible between process enumeration and termination; inherent
  to the snapshot-based approach.
- This is a terminal interface; there is no background service, voice interface,
  or desktop tray in Phase 1.

---

## Branch information

| Item | Value |
|------|-------|
| Phase 1 branch | `anish/phase-1-hardening` |
| Base | `main` |
| Phase 1 report | `reports/PHASE_1_IMPLEMENTATION_REPORT.md` |

---

## Phase 2 status

Phase 2 is in progress on `anish/phase-2-intelligence`, based on Phase 1 commit
`924c1f8fc0d0eefa673d2d9e9be25c97acf22d0b`.

- Structured intent classification is available through
  `classify_intent_structured()`. The orchestrator uses its deterministic intent
  category for the existing route decisions and exposes the latest `rule_id` and
  rationale through `last_intent_classification`. Confidence is a deterministic
  rule-specificity heuristic, not a probability, and is never used for approval.
- `MemoryRetriever.retrieve_with_semantic_fallback()` embeds the query and up to
  500 recent local memory records using the configured Ollama embedding model
  (default `embeddinggemma`). Semantic candidates are ordered by cosine
  similarity; deduplicated keyword results are appended. Ollama request failures
  fall back to keyword retrieval, and individual memory failures skip that
  candidate. Mocked tests prove semantic-only recall without an Ollama server.
- Memory embeddings are cached in bounded process memory by host, model, memory
  ID, and update timestamp. The orchestrator uses semantic fallback only before
  ordinary conversational responses and LLM-planned tasks. Explicit memory
  commands and deterministic application/file/browser routes bypass embedding
  work. Memory records are JSON-encoded user-level reference data, not system
  policy; embedded instructions cannot authorize tools or bypass approval.
  Persistent vector caching/indexing and end-to-end live-model evaluation remain
  open, so semantic memory is integrated but not yet complete.
- Persistent audit storage, persistent approvals, and per-tool/session rate
  limiting remain planned work; they are not implemented by this checkpoint.

The Phase 2 verification report is in
[`reports/PHASE_2_IMPLEMENTATION_REPORT.md`](reports/PHASE_2_IMPLEMENTATION_REPORT.md).
