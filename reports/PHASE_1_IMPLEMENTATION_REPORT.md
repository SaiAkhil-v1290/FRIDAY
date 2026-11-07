# FRIDAY — Phase 1 Implementation Report
## Architecture Stabilization & Security Hardening

**Branch:** `anish/phase-1-hardening`  
**Date:** 2026-11-07  
**Python:** 3.12  
**Virtual environment:** `.venv`

---

## A. Executive Summary

Phase 1 delivered targeted security hardening and architectural stabilization of the FRIDAY local AI assistant. The work preserved all existing functionality while systematically closing safety gaps across the full request-execution pipeline.

**Key outcomes:**

| Metric | Before | After |
|--------|--------|-------|
| Total tests | 267 | 303 |
| Test failures | 24 | 0 |
| Test errors | 0 | 0 |
| New security tests | — | 36 |
| Files modified | — | 10 |
| New files created | — | 2 |

All 303 tests pass. The 24 pre-existing failures were caused by a Windows 8.3 short-path mismatch in the test fixture (not a code bug) and are fixed. No existing test was removed or weakened.

---

## B. Initial Repository Assessment

### Baseline test run (before any changes)

```
Ran 267 tests in 125.058s
FAILED (failures=24)
```

**Root cause of all 24 failures:**  
`tempfile.mkdtemp()` on Windows can return a short 8.3 path such as `C:\Users\ANISHR~1\AppData\Local\Temp\...` while `Path.resolve()` and test assertions use the long form `C:\Users\Anish Reddy\...`. The mismatch caused every file-path comparison in `test_file_discovery.py` to fail.

**Fix applied:** `tests/test_file_discovery.py` line 26 — wrap the temp-dir result with `str(Path(tempfile.mkdtemp(...)).resolve())`.

### Security gaps identified

| Area | Finding |
|------|---------|
| `config.py` | No validation of numeric env vars; invalid URLs silently accepted |
| `security/permissions.py` | Simple boolean approval flags; no request identity, expiry, or replay protection |
| `core/executor.py` | Argument validation missing at execution time; audit trail absent |
| `core/planner.py` | Mutated caller's plan object in-place; no approval-id binding |
| `core/orchestrator.py` | No command-line path to approve/reject pending actions |
| `tools/files.py` | TOCTOU window between path check and file open; no document-size limits |
| `ai/schemas.py` | No per-tool argument schema validation; arbitrary dict accepted |
| `tools/registry.py` | Policies lacked `risk_level` / `category` metadata |
| Audit logging | No structured audit trail existed |

---

## C. Task-by-task Implementation Report

### TASK 1 — Architecture audit and stabilization

**Files changed:** `core/executor.py`, `core/planner.py`, `tools/registry.py`

**Findings:**
- `PlanExecutor.execute_action()` had a monolithic inline try/except block mixing dispatch, approval, and argument handling.
- `validate_plan()` in `core/planner.py` mutated the caller's `TaskPlan` object directly — a subtle bug causing state corruption when a plan was re-validated.
- Tool policies in `tools/registry.py` lacked structured metadata.

**Changes:**
- Extracted `_dispatch_action()` as a separate method in `PlanExecutor`, separating dispatch from policy enforcement.
- Added `plan = plan.model_copy(deep=True)` at the top of `validate_plan()` to prevent mutation.
- Extended `TOOL_POLICIES` entries with `risk_level`, `category`, and `description` fields.
- Added `get_tool_policy(name: str) -> dict | None` helper.

**Result:** Execution pipeline responsibilities are now clearly separated: policy check → argument validation → dispatch → post-execution audit.

---

### TASK 2 — Intent routing and schema validation

**Files changed:** `ai/schemas.py`

**Findings:**
- `ProposedAction.arguments` accepted any `dict` without per-tool validation.
- No mechanism to bind a validated approval token to a specific action.
- `TaskPlan.risk_level` did not include `"critical"` as a valid level.

**Changes:**
- Added `approval_id: str | None = None` field to `ProposedAction`.
- Added field validators: empty tool names rejected; arguments must be a dict with non-empty string keys.
- Added `"critical"` to the `risk_level` literal set.
- Implemented per-tool Pydantic argument schemas (`extra="forbid"`) for all 14 registered tools:
  - `OpenApplicationArgs`, `CloseApplicationArgs`, `CheckApplicationArgs`
  - `BrowserSearchArgs`, `BrowserNavigateArgs`, `BrowserPageInfoArgs`
  - `FileSearchArgs`, `SearchFileContentsArgs`, `FilePathArgs`, `FolderPathArgs`
  - `DeleteFilesArgs`, `EmailArgs`
- Added `TOOL_ARGUMENT_SCHEMAS` registry dict mapping tool names to their schema class.
- Added `validate_action_arguments(tool, arguments) -> (bool, str | None)` — deterministic validation returning a typed result; never calls the LLM.

**Result:** Invalid, incomplete, malformed, and unexpected arguments are rejected deterministically before any tool executes.

---

### TASK 3 — Configuration validation

**Files changed:** `config.py`

**Findings:**
- Numeric env vars (`OLLAMA_TIMEOUT`, `MAX_HISTORY_MESSAGES`) were parsed with no validation; a non-numeric value would raise an unhandled `ValueError` at startup.
- URLs were not validated for scheme or emptiness.
- No configuration values existed for the new Phase 1 security parameters.

**Changes:**
- Added `ConfigurationError` — explicit exception class for startup failures.
- Added private validation helpers:
  - `_validate_url(value, name)` — rejects empty, non-http/https, and malformed URLs.
  - `_validate_float(value, name, min_val, max_val)` — validates and bounds-checks floats.
  - `_validate_int(value, name, min_val, max_val)` — validates and bounds-checks ints.
  - `_validate_string(value, name, pattern)` — rejects shell metacharacters in model names.
- Added `load_settings(env=None)` — accepts an optional env dict for injection in tests; falls back to `os.environ`.
- Extended `Settings` frozen dataclass with Phase 1 security fields:
  - `max_document_size_bytes` (default: 10 MB)
  - `max_extracted_chars_per_doc` (default: 500,000)
  - `max_extraction_units_per_doc` (default: 5,000)
  - `approval_timeout_seconds` (default: 300)
- **Security rule:** Invalid security-related settings raise `ConfigurationError` and halt startup — they are never replaced with unsafe defaults.

**Result:** All configuration values are validated at startup. Errors produce clear, actionable messages without exposing secrets.

---

### TASK 4 — Formal permission and approval workflow

**Files changed:** `security/permissions.py`, `core/planner.py`, `core/orchestrator.py`, `main.py`

**Findings:**
- `security/permissions.py` contained simple boolean flags with no request identity, no timestamps, no expiry, and no replay protection.
- `core/planner.py` had no mechanism to generate or attach approval tokens to actions.
- `core/orchestrator.py` had no path for the user to approve or reject pending requests.

**Changes — `security/permissions.py` (complete rewrite):**
- `ApprovalStatus` enum: `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `EXECUTED`, `FAILED`.
- `ApprovalRequest` dataclass: `approval_id` (UUID4), `tool`, `arguments` (deep-copied at creation), `risk_level`, `permission_category`, `explanation`, `created_at`, `expires_at`, `status`, `execution_status`, `error`.
- `ApprovalManager` class:
  - `create_request()` — generates a unique, time-limited request; arguments are deep-copied to prevent later tampering.
  - `approve_request()` — transitions PENDING → APPROVED; rejects expired, already-processed, or unknown requests.
  - `reject_request()` — transitions PENDING → REJECTED.
  - `validate_and_consume()` — atomically validates tool identity, argument equality, and not-expired; transitions APPROVED → EXECUTED; replay-safe (single-use).
  - `mark_failed()` / `mark_completed()` — post-execution lifecycle hooks.
  - `list_requests()` / `get_audit_log()` — inspection interface.
- Helper functions: `requires_approval(tool)`, `get_tool_risk(tool)`, `get_tool_category(tool)`.

**Changes — `core/planner.py`:**
- After policy validation identifies approval-required actions, `approve_request()` creates a formal `ApprovalRequest` and stores its `approval_id` on the `ProposedAction`.
- Deduplication: does not create duplicate requests if the action already has a valid PENDING approval_id.

**Changes — `core/orchestrator.py`:**
- Handles `"approve <UUID>"` / `"reject <UUID>"` user input as special commands before intent classification.
- `approve` path: calls `approval_manager.approve_request()` then executes via `PlanExecutor`.
- `reject` path: calls `approval_manager.reject_request()` and confirms to the user.

**Changes — `main.py`:**
- Approval display now shows approval IDs with usage instructions: `"run 'approve <id>' to execute or 'reject <id>' to cancel"`.

**Result:** Complete, single-use, time-bounded approval lifecycle. The LLM cannot approve its own actions. Expired and replayed requests are blocked deterministically.

---

### TASK 5 — Execution-level policy enforcement

**Files changed:** `core/executor.py`

**Findings:**
- Argument validation was absent at execution time; the executor trusted whatever the planner had assembled.
- Unregistered tools were blocked but audit events were not emitted.
- Multi-step failures did not emit structured audit events.

**Changes:**
- Execution pipeline in `execute_action()` enforced in this order:
  1. `is_registered_tool()` check — DENIED event logged if unknown.
  2. `must_require_approval` check:
     - If `approval_id` is present: `validate_and_consume()` — blocks pending, mismatched, altered, and replayed requests.
     - If no `approval_id` and approval required: returns `APPROVAL_REQUIRED`.
  3. `IMPLEMENTED_EXECUTION_TOOLS` check.
  4. **`validate_action_arguments()`** — rejects invalid/missing/extra arguments; returns `INVALID_ARGUMENTS`.
  5. `_dispatch_action()` — executes the tool.
  6. `approval_manager.mark_completed()` or `mark_failed()`.
  7. `audit_logger.log()` with result.
- Multi-step execution: `PlanExecutor.execute_plan()` stops on first step failure when a step is a dependency of subsequent steps (existing stop-on-failure logic preserved and audited).
- Tool failures return structured error responses; they are never reported as success.

**Result:** Every execution path goes through deterministic validation. The model cannot bypass policy by generating an alternative plan.

---

### TASK 6 — File access security hardening

**Files changed:** `tools/files.py`

**Findings:**
- A TOCTOU (time-of-check/time-of-use) window existed: path safety was validated once during planning, but the actual `os.startfile()` / `subprocess.Popen()` calls happened without re-validation.
- No defense against symlink-based path traversal at file-open time.

**Changes:**
- `open_local_file()`: added re-validation block immediately before `os.startfile()` — re-resolves the path (following symlinks) and re-checks safe-root containment.
- `reveal_local_file()`: same TOCTOU re-validation before `subprocess.Popen(['explorer', '/select,', path])`.
- `open_local_folder()`: same TOCTOU re-validation before OS open call.
- All three functions now return a structured error if the re-validation fails rather than proceeding.

**Known remaining limitation:** Re-validation reduces but cannot fully eliminate TOCTOU risk on Windows because there is no atomic file-open-with-path-validation primitive at the OS level. This is documented as a known limitation.

---

### TASK 7 — Document extraction resource limits

**Files changed:** `tools/files.py`

**Findings:**
- Extraction functions (`extract_pdf_units`, `extract_docx_units`, etc.) had no limits on file size, character count, or unit count — a large or malicious document could exhaust memory or CPU.
- Encrypted PDFs caused unhandled exceptions.
- No file-size check before dispatching to extractors.

**Changes:**
- `extract_text_file_units()`: added `max_chars`, `max_units` parameters with early-exit loop.
- `extract_pdf_units()`: added `max_chars`, `max_units`, `max_pages` parameters; handles `is_encrypted`; full exception wrapper.
- `extract_docx_units()`: added `max_chars`, `max_units`, `max_paragraphs` parameters; full exception wrapper.
- `extract_pptx_units()`: added `max_chars`, `max_units`, `max_slides` parameters; full exception wrapper.
- `extract_xlsx_units()`: added `max_chars`, `max_units`, `max_rows` parameters; full exception wrapper.
- `extract_searchable_units()`: reads `max_document_size_bytes` from `settings` and **skips files exceeding the limit** before dispatching; passes `max_chars` / `max_units` from settings to all extractors.

**Default limits (from `config.py`):**

| Setting | Default |
|---------|---------|
| `MAX_DOCUMENT_SIZE_BYTES` | 10,485,760 (10 MB) |
| `MAX_EXTRACTED_CHARS_PER_DOC` | 500,000 |
| `MAX_EXTRACTION_UNITS_PER_DOC` | 5,000 |

---

### TASK 8 — Process termination safety

**Files reviewed:** `tools/application.py`

**Findings:**
- Allowlist enforcement exists: only applications registered in the tool policy registry can be terminated.
- Protected-app policy is enforced: applications marked `closable: false` are rejected.
- Alias resolution prevents bypassing allowlist via alternate names.
- PID-based termination (`terminate_process_by_pid(0)`) on Windows raises `PermissionError` for system PIDs (0, 4), which is caught and returns `False` — acceptable OS-level protection.

**Changes:** No code changes required. Existing safeguards are sound. Documented known limitations:
- **PID reuse risk:** The snapshot from `get_running_process_names()` and the subsequent termination call are not atomic. A PID could theoretically be reassigned between snapshot and kill. This is inherent to the snapshot-based approach and cannot be eliminated without OS-level support.
- **Windows PID 0/4 protection:** Relies on OS-level `PermissionError` rather than explicit PID blocklist — acceptable and correct behavior.

---

### TASK 9 — Audit logging

**Files created:** `security/audit.py`

**Findings:** No structured audit trail existed. Failures and security events were logged inconsistently or not at all.

**Implementation — `AuditLogger` class:**
- Structured `AuditEvent` dataclass: `timestamp`, `event_type`, `tool`, `arguments`, `status`, `details`, `approval_id`.
- Event types: `TOOL_EXECUTED`, `TOOL_DENIED`, `TOOL_FAILED`, `APPROVAL_CREATED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`, `APPROVAL_EXPIRED`, `APPROVAL_CONSUMED`, `APPROVAL_REPLAY_BLOCKED`, `CONFIG_LOADED`, `SECURITY_VIOLATION`.
- Automatic redaction of fields whose keys contain `"password"`, `"secret"`, `"token"`, `"key"`, or `"cred"` — values replaced with `"[REDACTED]"`.
- Long string values (> 200 chars) truncated to prevent log flooding.
- Bounded in-memory log (default: 2,000 entries) with oldest-entry eviction.
- Global `audit_logger` singleton imported by `core/executor.py`.

---

### TASK 10 — Documentation

**Files changed/created:** `README.md`, `reports/PHASE_1_IMPLEMENTATION_REPORT.md` (this file)

- `README.md` updated to reflect FRIDAY Phase 1 (replacing stale JARVIS Milestone 1 branding), document all new security features, approval workflow, permission enforcement, file-access boundaries, document-extraction limits, process-termination safeguards, environment configuration, and how to run tests.
- This report created under `reports/`.

---

## D. Security Improvements Summary

| Area | Before Phase 1 | After Phase 1 |
|------|---------------|--------------|
| Configuration validation | None — invalid values cause confusing crashes | Explicit validation with clear error messages at startup |
| Argument validation | Arguments accepted as raw dicts | Per-tool Pydantic schemas with `extra="forbid"` validated at both planning and execution time |
| Approval workflow | Simple boolean flags | UUID-keyed, time-bounded, single-use, replay-safe approval requests |
| Execution-time policy | Minimal registration check | 4-stage validation: registration → approval token → arg schema → dispatch |
| Audit trail | None | Structured audit events with automatic secret redaction |
| TOCTOU file access | Single path check at plan time | Re-validation immediately before every file-system call |
| Document extraction | Unbounded — could OOM on large files | File-size limit (10 MB) + char limit (500k) + unit limit (5,000) |
| LLM authority boundary | LLM output partially trusted for execution | LLM output never directly executes; every action validated deterministically |

---

## E. Test Report

### Baseline (before Phase 1)

```
Ran 267 tests in 125.058s
FAILED (failures=24, errors=0)
```

Root cause: Windows 8.3 short-path mismatch in `tests/test_file_discovery.py`.

### After Phase 1 fix and additions

```
Ran 303 tests in ~147s
OK (failures=0, errors=0)
```

### New tests added (`tests/test_phase1_security_hardening.py`)

| Category | Count |
|----------|-------|
| Config validation | 8 |
| Schema / intent validation | 5 |
| Approval workflow | 7 |
| Executor policy enforcement | 3 |
| File access security | 6 |
| Document extraction limits | 3 |
| Process termination safety | 3 |
| Audit logging | 1 |
| **Total new** | **36** |

### Coverage of approval lifecycle

| Scenario | Test |
|----------|------|
| Create approval request | ✅ |
| Approve request | ✅ |
| Reject request | ✅ |
| Expire request | ✅ |
| Single-use (replay blocked) | ✅ |
| Tool name mismatch | ✅ |
| Argument tampering | ✅ |
| Duplicate approval call | ✅ |

---

## F. File Change Summary

| File | Status | Description |
|------|--------|-------------|
| `config.py` | Modified | Full validation, `ConfigurationError`, Phase 1 security settings, injectable env |
| `ai/schemas.py` | Modified | Per-tool arg schemas, `validate_action_arguments()`, `approval_id` field, `"critical"` risk level |
| `core/executor.py` | Modified | 4-stage execution policy, approval-token consumption, audit logging, `_dispatch_action()` extraction |
| `core/planner.py` | Modified | Approval-request creation, `approval_id` binding, deep-copy mutation fix |
| `core/orchestrator.py` | Modified | `approve`/`reject` command handling, `import re` |
| `main.py` | Modified | Approval-ID display with usage instructions |
| `security/permissions.py` | Modified | Complete rewrite — `ApprovalStatus`, `ApprovalRequest`, `ApprovalManager` |
| `tools/files.py` | Modified | TOCTOU re-validation, document-size limits, extraction resource limits |
| `tools/registry.py` | Modified | `risk_level`, `category`, `description` metadata; `get_tool_policy()` helper |
| `tests/test_file_discovery.py` | Modified | Windows short-path fix (`Path(...).resolve()`) |
| `security/audit.py` | Created | `AuditLogger`, `AuditEvent`, global `audit_logger` singleton |
| `tests/test_phase1_security_hardening.py` | Created | 36 Phase 1 security tests |
| `reports/PHASE_1_IMPLEMENTATION_REPORT.md` | Created | This document |

---

## G. Remaining Issues and Known Limitations

### Acceptable / documented

| Item | Detail |
|------|--------|
| TOCTOU residual risk | Re-validation at file-open time reduces but cannot fully eliminate TOCTOU risk on Windows (no atomic file-open-with-containment primitive at OS level) |
| PID reuse risk | Snapshot-based process enumeration is not atomic with termination; inherent to the approach |
| Windows PID 0/4 protection | Relies on OS-level `PermissionError` rather than explicit PID blocklist; correct and acceptable |
| `allow_approved=True` bypass | Legacy parameter preserved in `PlanExecutor.execute_action()` for backward compatibility with pre-Phase-1 tests; should be deprecated in a future phase |
| In-memory audit log | Audit events are stored in RAM only; not persisted across restarts. Phase 2 should write them to SQLite |
| In-memory approval store | `ApprovalManager` stores requests in RAM; approved requests are lost on restart. Acceptable for Phase 1 CLI use |

### Not in scope (Phase 2+)

- Persistent audit log (SQLite or file)
- Semantic memory indexing
- Voice interface
- Desktop approval UI / tray notifications
- Scheduled task orchestration
- Screen capture / OCR

---

## H. Phase 1 Acceptance Checklist

| Task | Status | Notes |
|------|--------|-------|
| TASK 1 — Architecture audit and stabilization | ✅ PASS | Deep-copy fix, `_dispatch_action()` extraction, registry metadata |
| TASK 2 — Intent routing and schema validation | ✅ PASS | 14 per-tool schemas, `validate_action_arguments()`, `approval_id` field |
| TASK 3 — Configuration validation | ✅ PASS | `ConfigurationError`, URL/int/float/string validators, 4 new settings |
| TASK 4 — Formal permission and approval workflow | ✅ PASS | UUID-keyed, time-bounded, single-use, replay-safe; approve/reject commands |
| TASK 5 — Execution-level policy enforcement | ✅ PASS | 4-stage validation at execution time; audit events on all paths |
| TASK 6 — File access security hardening | ✅ PASS | TOCTOU re-validation in all 3 file-open functions |
| TASK 7 — Document extraction resource limits | ✅ PASS | File-size + char + unit limits across all 5 extractors |
| TASK 8 — Process termination safety | ✅ PASS | Allowlist, protected-app policy, alias resolution; OS protects system PIDs |
| TASK 9 — Audit logging | ✅ PASS | Structured events, automatic redaction, bounded memory |
| TASK 10 — Documentation | ✅ PASS | README updated, this report created |
| Baseline fix (Windows short-path) | ✅ PASS | `Path.resolve()` applied in `test_file_discovery.py` |
| All original tests still pass | ✅ PASS | 267 original tests: 0 failures |
| New Phase 1 tests added and passing | ✅ PASS | 36 new tests: 0 failures |

---

## I. Git Information

| Item | Value |
|------|-------|
| Branch | `anish/phase-1-hardening` |
| Base branch | `main` |
| Commits | 1 (Phase 1 implementation, all changes) |
| Push status | Not pushed — awaiting explicit authorization |

---

## J. Recommendation for Phase 2

Phase 1 established the security foundation. Recommended Phase 2 priorities:

1. **Persistent audit log** — write `AuditEvent` records to SQLite (`data/audit.db`) for post-session forensics.
2. **Persistent approval store** — save pending approval requests across restarts (critical for voice/background operation).
3. **Semantic memory** — integrate vector search over long-term memory for grounded responses.
4. **Deprecate `allow_approved=True`** — replace the legacy bypass with the structured `approval_id` path in all callers.
5. **Rate limiting** — add per-tool and per-session rate limits to prevent runaway LLM-generated plans.
6. **Sandboxed subprocess execution** — wrap `subprocess.Popen` calls in a restricted subprocess with explicit capability grants.
