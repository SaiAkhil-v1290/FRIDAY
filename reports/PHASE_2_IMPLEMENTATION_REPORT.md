# FRIDAY Phase 2 Orchestration Integration

**Date:** 2026-11-11
**Branch:** `anish/phase-2-intelligence`
**Base:** `924c1f8fc0d0eefa673d2d9e9be25c97acf22d0b` (`security: finalize phase 1 hardening and approval enforcement`)
**Status:** Phase 2 implementation committed and pushed; report update is a separate documentation-only follow-up.

## Git verification and proposed contents

The Phase 1 base is an ancestor of Phase 2 HEAD. `git merge-base HEAD anish/phase-1-hardening` equals the base SHA. At the start of this increment, committed diff `anish/phase-1-hardening...HEAD` was empty; all Phase 2 work was uncommitted.

Files in the Phase 2 implementation commit:

- `.env.example`
- `README.md`
- `ai/conversation.py`
- `ai/intent.py`
- `ai/llm.py`
- `ai/prompts.py`
- `config.py`
- `context.md`
- `core/orchestrator.py`
- `memory/grounding.py`
- `memory/retrieval.py`
- `tests/test_memory_retrieval.py`
- `tests/test_orchestrator_memory_integration.py`
- `reports/PHASE_2_IMPLEMENTATION_REPORT.md`

A personal, untracked `DT20257018510_Application_Form.pdf` remains in the worktree and was excluded from both the implementation commit and push. No `.env`, database, secret, or generated artifact was committed.

## Implemented

### Semantic retrieval

`MemoryRetriever.retrieve_with_semantic_fallback()` now attempts embedding generation even when keyword retrieval returns no candidates. It embeds the query and up to 500 most recently listed long-term-memory records with the configured Ollama embedding model; `embeddinggemma` remains the default. It computes cosine similarity only for equal-dimension finite vectors with nonzero norms, clamps results to `[-1, 1]`, drops nonpositive matches, and orders semantic candidates by descending score with memory ID as a stable tie-break.

The merge is deterministic: semantic hits first in similarity order, then keyword-ranked hits not already present, deduplicated by source memory ID and bounded by result count and `MAX_MEMORY_CHARS`. It returns original `Memory` objects, preserving IDs and provenance. Request embedding failure returns keyword results. A failed candidate embedding skips only that candidate; it does not abort retrieval. If Ollama is unavailable, the method returns keyword results and does not report a semantic hit. Empty databases return without contacting Ollama.

A bounded per-retriever in-memory LRU-like cache stores memory vectors (maximum 512) keyed by Ollama host, embedding model, memory ID, and `updated_at`. Uncached candidate text is embedded in batches of 32; a failed batch is retried record-by-record so one bad record cannot discard valid candidates. Query embeddings are not cached. This avoids repeat memory-vector generation during a process lifetime without adding a database migration or vector-database dependency.

### Configuration and privacy

`OLLAMA_EMBEDDING_MODEL` configures the embedding model (`embeddinggemma` by default). `OLLAMA_HOST` accepts HTTP(S) hosts but now rejects non-loopback hosts unless `ALLOW_REMOTE_OLLAMA=true` is explicitly set. When opted in, prompts and retrieved private-memory text are sent to that configured endpoint; FRIDAY cannot determine whether the endpoint is a public cloud service. The environment template and README make this explicit. Loopback literals and `localhost` remain allowed by default.

### Structured intent

`IntentClassificationResult` includes `rule_id`. The existing deterministic classifier records the exact branch/pattern that matched, and `reason` names that rule while stating that intent classification does not authorize execution. `classify_intent(text)` retains its previous return type and behavior through a wrapper around the traced implementation. `JarvisOrchestrator` now uses `classify_intent_structured()` for the same existing deterministic routing categories and exposes the latest result as `last_intent_classification` for diagnostics.

`confidence` is an uncalibrated deterministic rule-specificity heuristic, not a model probability. It is not used for authorization or approval. Clarification routing remains based on the deterministic intent category; planner validation and approval policy remain authoritative.

### Orchestration and prompt boundary

The orchestrator requests semantic memory only on two model-consumption paths: normal conversational responses and LLM-generated plans. Explicit memory store/forget/recall commands, prompt-injection blocks, missing-input clarifications, contextual file responses, and deterministic application/file/browser operations return or execute without invoking semantic retrieval. Unexpected retrieval exceptions retry the keyword retriever; if that also fails, the assistant proceeds with empty memory context.

`MemoryGrounder` now formats each source record as JSON with its original memory ID, category, creation/update timestamps, and content. `ConversationManager` and `OllamaClient` keep policy in the system role and place the grounded memory context in the user-level request as JSON-encoded reference data. Both trusted system prompts explicitly say memory content is untrusted: embedded instructions cannot reveal private data, change permissions, authorize tools, or bypass approvals. Deterministic plan validation and `PlanExecutor` remain unchanged.

## Tests and exact results

All commands ran from the repo root with `.venv` Python.

1. Focused integration, memory grounding/retrieval, and deterministic application-routing tests:

```powershell
cd "c:\Users\Anish Reddy\OneDrive\Desktop\Friday"; .\.venv\Scripts\python.exe -m unittest tests.test_orchestrator_memory_integration tests.test_memory_grounding tests.test_memory_retrieval tests.test_conversational_application
```

Result: `Ran 50 tests in 25.583s` — `OK`. The mocked Ollama client proves semantic-only conversation recall when keyword retrieval returns no results, keyword fallback on Ollama failure, unexpected retrieval-exception handling, empty storage, prompt-injection memory framing in both chat and planning, memory command preservation, deterministic route bypass, no action execution from memory, record ID provenance, and structured intent diagnostics. No Ollama service is needed.

2. Phase 1 security regression:

```powershell
cd "c:\Users\Anish Reddy\OneDrive\Desktop\Friday"; .\.venv\Scripts\python.exe -m unittest tests.test_phase1_security_hardening
```

Result: `Ran 37 tests in 1.284s` — `OK`. Expected malformed-PDF fixture diagnostics appeared.

3. Complete unittest suite:

```powershell
cd "c:\Users\Anish Reddy\OneDrive\Desktop\Friday"; .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Result: `Ran 327 tests in 160.386s` — `OK`, 0 failures and 0 errors. This is 10 tests above the previous 317-test checkpoint. Existing output includes malformed-PDF diagnostics and subprocess `ResourceWarning`s; tests passed.

## Remaining limitations

- Semantic retrieval now runs in normal conversation and LLM-planned-task flows, but tests use mocked embeddings; there is no live-model relevance evaluation.
- Candidate search is limited to the newest 500 records. Older relevant memories can be missed when the store grows beyond that bound.
- Memory vector caching is process-local, non-persistent, and limited to a retriever instance. Cold start recomputes embeddings; there is no invalidation other than ID/update timestamp changing the cache key and no database migration/index yet.
- Per-record failures are skipped. If some candidate embeddings fail, partial semantic results are returned and keyword-only results are appended; the response does not expose a status explaining that candidate coverage was partial.
- The current `list_memories()` maximum is 500, matching the semantic cap. Persistent SQLite vector storage or a separate local index should be proposed after measuring database size and embedding latency. A conservative next migration would add a versioned `memory_embeddings` table keyed by memory ID, content/update fingerprint, model name, and vector format; rebuild lazily on missing/stale records; retain source memory rows as authoritative; and keep keyword fallback on migration or model errors. A vector database is not justified yet.
- Cosine similarity is only a ranking signal; no relevance threshold has been calibrated. The current positive-score filter can return weak positive matches.
- Remote Ollama is an explicit user opt-in but the application cannot attest to endpoint ownership, transport policy, or whether the endpoint is cloud-hosted.
- Structured intent confidence remains heuristic. Its category/rule diagnostics are integrated, but neither confidence nor intent grants authorization; approval and execution checks remain independent.

## Orchestration Integration and Security Review

The implementation keeps interpretation, authorization, and execution separate:

1. Structured intent returns the same deterministic category as the legacy classifier; `last_intent_classification` exposes rule/reason diagnostics. Confidence is not used by routing or policy code.
2. Semantic retrieval runs only before a conversational response or an LLM-generated plan. Explicit memory commands and deterministic file/application/browser paths bypass embedding generation.
3. `MemoryGrounder` serializes each stored record as JSON with ID, category, timestamps, and content. Both model clients keep policy in the system role and place retrieved context in user-level JSON data. System prompts prohibit following memory instructions or treating memory as permission.
4. `validate_plan()` and `PlanExecutor` remain unchanged and authoritative for registered tools, argument validation, risk, explicit approval IDs, expiry, and execution. Integration tests verify memory cannot authorize execution and Phase 1 security regressions still pass.
5. No executor, approval manager, permission policy, Akhil-owned computer-control, voice, or desktop module was modified.

## Notion

The existing FRIDAY PLAN page was updated in the Phase 2 structured-intent, semantic-retrieval, and test-evidence entries. Semantic retrieval is described as integrated for conversation/planning but still limited by no persistent index and no live-model evaluation. Phase 2 checkboxes remain unchecked and the exit gate is unchanged. Phase 1 acceptance records were not edited.

## Commit and Push

- Branch: `anish/phase-2-intelligence`.
- Implementation commit: `9cca45a48281a7a244d12d345d7c22fb8c764b35` (`feat: integrate semantic memory and structured intent`).
- Push: successful to `origin/anish/phase-2-intelligence`; `git ls-remote origin refs/heads/anish/phase-2-intelligence` returned the implementation SHA above.
- The implementation commit contains the 14 files listed in the Git verification section. This report's final commit/push record is a separate documentation-only follow-up.
- The Phase 1 branch and `main` were not modified or merged. No Phase 3 or Phase 4 work was started.
