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
OLLAMA_TIMEOUT=120

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

---

## Running the tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

Expected result: `Ran 303 tests` — `OK` (0 failures, 0 errors).

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

## Phase 2 preview

The next phase will add:
- Persistent audit log (SQLite)
- Persistent approval store (survive restart)
- Semantic memory and vector search over long-term memory
- Rate limiting per tool and per session
