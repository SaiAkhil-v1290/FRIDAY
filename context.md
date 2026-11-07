# Project Context

## Overview
J.A.R.V.I.S. is a local personal assistant built in Python.
- **Milestone 1**: Local brain in planning-only mode with deterministic intent routing, conversation handling, task planning, and RAM-only working memory.
- **Milestone 2A**: Persistent local SQLite long-term memory engine (`data/jarvis.db`) managing facts, user profile information, and structured preferences.
- **Milestone 2B**: Explicit user memory commands (`remember that ...`, `forget that ...`, `what do you remember about me?`) routed deterministically without LLM hallucination or automatic conversation storage.
- **Milestone 2C**: Automatic relevant long-term memory retrieval (`MemoryRetriever`) to augment LLM conversation and task planning with relevant background context, strictly without automatic conversation saving.
- **Milestone 2D**: Memory-grounded factual responses (`MemoryGrounder`) preventing hallucination of technologies, libraries, or project stages, while allowing suggestions clearly distinguished from stored facts.
- **Milestone 3A & 3B**: Safe action execution framework (`PlanExecutor`) and safe local application launcher (`launch_application`) with deterministic allowlists, non-shell execution, and strict approval enforcement.
- **Milestone 4A**: Safe browser search execution (`search_browser` in `tools/browser.py`). Executes search queries against Google with strict URL encoding (`quote_plus`), query length validation ($\le 500$), and non-shell dispatch via Python's standard `webbrowser` controller. Implemented in `IMPLEMENTED_EXECUTION_TOOLS` without arbitrary DOM automation or shell execution.
- **Milestone 4B**: Safe read-only local file discovery (`discover_files` in `tools/files.py`). Allows searching, filtering by extension, and listing directory contents deterministically inside explicitly allowed safe roots (`Desktop`, `Documents`, `Downloads`). Fully bounded by `MAX_RESULTS = 100`, strict path resolution containment (`resolve()`), and symlink escape checks. Modification, deletion, renaming, moving, and shell execution remain strictly prohibited.
- **Milestone 4B.4**: Safe local file content search (`search_file_contents` in `tools/files.py`). Safely scans supported text-based files (`.txt`, `.py`, `.md`, `.json`, `.csv`, `.html`, `.css`, `.js`, `.ts`, etc.) line-by-line within safe roots to find matching text and return bounded snippets with line numbers. File sizes are capped at 5 MB, binary files are excluded, developer directories are pruned, and search queries are treated as literal text (no regex/eval/shell). Explicit browser searches strictly retain priority over content searches.
- **Milestone 4B.5 & 4B.5.1**: Safe document content search (`.pdf`, `.docx`, `.pptx`, `.xlsx`). Extract and search document content using `pypdf`, `python-docx`, `python-pptx`, and `openpyxl`. Follow-up search context preservation across queries.
- **Milestone 4B.6, 4B.6.1 & 4B.6.2**: Safe file opening & contextual file selection (`open_file` in `tools/files.py`). Enables opening files requested by name or contextually from previous search results (`"Open the first result"`, `"Open that PDF"`, `"Open it"`). Resolves paths strictly within `SAFE_SEARCH_ROOTS`, prevents guessing when multiple files match by prompting for clarification with a numbered list, validates selection indices without arbitrary choices, and executes safely via `os.startfile` on Windows or `xdg-open` with `shell=False`.
  - **Ordinal-Only and Numeric Selection Routing (Milestone 4B.6.2)**: Supports standalone ordinal phrases (`open first`, `open fifth`, `open second`, bare `first`, bare `fifth`) in addition to existing patterns (`open first one`, `open result 1`, `open 1`, `open #14`). Deterministic resolution runs strictly BEFORE LLM planning; if an active selection or search result context exists, it resolves directly to the matching candidate without calling the LLM or risking hallucinated actions/sample files. If no active context exists, it returns a clean clarification (`"I don't have a recent file list to select result {idx} from..."`) with zero computer actions.
  - **Numeric & Ordinal Contextual File Selection (Bug 1 & 4 Fix)**: Bare numeric and ordinal inputs (`5`, `13`, `open 5`, `open 13`, `open #5`, `open #14`, `open number 5`, `open result number 5`, `result 5`, `open result 5`, `open the 5th one`, `open fifth one`, `open the fifth result`) are routed to `open_file` when recent file results exist in working memory rather than mistakenly treated as applications. If no file results exist in memory, returns a clean clarification: `"I don't have a recent file list to select result {idx} from. Please search for files first or specify the file name."` Purely numeric and index-like targets are strictly rejected in `extract_explicit_open_app_target` and `resolve_application`.
  - **Deterministic Active File-Selection Context Synchronization**: Enforces the invariant `DISPLAYED_FILE_SELECTION_LIST == ACTIVE_FILE_SELECTION_CONTEXT`. Whenever any file-selection clarification list or preview is presented (e.g. `Open my resume` yielding multiple candidates), the active selection context in `WorkingMemory` (`set_active_file_selection`) is updated with the exact candidate list displayed to the user. Subsequent bare numeric/ordinal commands (`open 1`, `open result 1`, `open first`) immediately resolve to the active clarification list rather than stale previous discovery sets.
  - **Deterministic Application Launch Argument Preservation (Bug 2 Fix)**: Fixed deterministic application launching (`Open Calculator`, `Open Notepad`) by ensuring `ProposedAction(tool="open_application", arguments={"application": explicit_app})` is always populated with the canonical application name. Added defensive validation and recovery in `validate_plan` (`core/planner.py`) to reconstruct the application parameter from `current_request` if missing or malformed, and strictly drop invalid actions without an application.
  - **Conversational Query Normalization (Bug 3 Fix)**: Strips conversational filler words (`my`, `the`, `file`, `document`, `a`, `an`) across discovery and contextual matching while preserving search terms and casing for exact searches, enabling natural queries like `Find my resume in Downloads` to find files like `Akhil_Resume(Updated).pdf` without over-fuzzing unrelated queries.


- **Milestone 4C.1**: Conversational File Reference Resolution.
  - **Conversational Reference Understanding**: Extended the local file interaction system so JARVIS understands conversational follow-ups to files and file discovery results:
    - **Single-Result Pronouns**: Requests like `Open it`, `Open that file`, `Can you open it?`, `Open that PDF`, `Open that document`, `Open that one` deterministically resolve to the uniquely matched file in active working memory context without calling the LLM or guessing.
    - **Multiple-Result Clarification Guard**: When multiple candidates match `Open it` or `Open that file`, JARVIS asks for clarification, generates zero computer actions, displays numbered candidates, and avoids arbitrarily choosing a file.
    - **Reopening Files (`Open it again`)**: `WorkingMemory` tracks the most recently opened file via `_last_opened_file` (`set_last_opened_file` / `get_last_opened_file`). Commands like `Open it again` immediately re-open that file safely without calling the LLM.
    - **Unique Descriptive References**: Commands such as `Open the updated one`, `Open the PDF one` parse descriptive adjectives/types and filter against active context. If exactly one candidate matches, it opens immediately without LLM involvement.
    - **Ambiguous Descriptive References**: If multiple candidates match a description (e.g., two resumes containing `updated`), JARVIS displays a clarification list and generates zero computer actions.
    - **Named & Called References**: Queries like `Open Akhil_Resume(Updated).pdf`, `Open Akhil Resume`, `Open the resume called Akhil`, or `Open the file named Akhil` resolve deterministically against active context or allowed safe search roots.
    - **No-Context Safety**: Pronoun requests with no recent context or search results (e.g. `Open it`) prompt for clarification (`"I don't have a recent file result to resolve \"it\" to. Please specify the file you want to open."`) with zero actions and no hallucinated LLM planning.
  - **Preserved Safety Invariants**: `DISPLAYED_FILE_SELECTION_LIST == ACTIVE_FILE_SELECTION_CONTEXT`, safe root boundaries (`Desktop`, `Documents`, `Downloads`), non-shell execution (`shell=False`, `os.startfile`), and ordinal/numeric selection compatibility (`open 1`, `open first`) are strictly maintained.

- **Milestone 4C.2**: Conversational File Navigation and Contextual Selection.
  - **Contextual Navigation Between Files**: Extended the file interaction system so JARVIS can navigate relative to an active candidate selection or discovery context:
    - **Relative Navigation Commands**: Deterministically parses `"Open the next one"`, `"Open next"`, `"Open the previous one"`, `"Open previous"`, `"Open the next file"`, `"Open the previous file"`, `"Open the next PDF"`, `"Open the previous PDF"`, `"Open the one after that"`, `"Open the one before that"` into `{"type": "relative", "direction": "next" | "previous", "target": "file" | "pdf" | "document"}`.
    - **Current Selection Tracking (`WorkingMemory`)**: Tracks `current_index` within `_active_selection_context`. Lifecycle methods `set_current_file_selection_index(idx)` and `get_current_file_selection_index()` maintain 1-based indexing bounded strictly by candidates length.
    - **Context Replacement Safety**: Creating or replacing active candidate selections (search results or clarification lists) resets `current_index` to `None`, guaranteeing that stale indexes from previous contexts are never navigated against newer contexts.
    - **Position Establishment**: Successfully opening a file from the active context via numeric selection (`open 2`), ordinal selection (`open second`), descriptive match (`open the updated one`), or named match establishes the current selection index.
    - **Boundary Safety**: Navigating past the last file (`open next` at boundary) or before the first file (`open previous` at boundary) returns a clear, safe message (`"You are already at the last file in the current selection."` / `"You are already at the first file in the current selection."`) with 0 actions and 0 LLM calls.
    - **No-Context & Unset Position Safety**: If no candidate context exists, returns a clarification prompt (`"I don't have a recent file list to determine what \"next\" refers to..."`). If an active list exists but no file has been selected yet, prompts the user (`"Please specify which file you want to start with from the current list."`) with 0 actions and 0 LLM calls.
    - **File-Type Relative Navigation**: Supports specific navigation such as `Open the next PDF`, skipping non-PDF files in the candidate list to reach the next matching file type.
    - **LLM Hallucination Bypass**: All relative navigation requests are intercepted before LLM planning and excluded from application launching allowlist checks, completely preventing false `open_application("next")` or hallucinated file actions.

- **Milestone 4C.3**: Conversational File Actions Using Last-Opened File Context.
  - **Conversational Actions on Recently Opened File**: Extended the assistant to safely and deterministically answer questions and trigger safe contextual actions regarding the file tracked in `WorkingMemory.get_last_opened_file()`:
    - **File Name (`get_name`)**: Queries like `"What is its name?"`, `"What's its name?"`, `"What is the file name?"`, `"Tell me the name of this file"`, `"What file is this?"` deterministically return the filename (e.g. `"The file is named Akhil_Resume(Updated).pdf."`) with 0 actions and 0 LLM calls.
    - **File Type & Extension (`get_type`)**: Queries like `"What type of file is it?"`, `"What file type is this?"`, `"What is its extension?"`, `"What is the file type?"` return the type and extension (e.g. `"This is a PDF file (.pdf)."`) using the known extension without guessing MIME types (0 actions, 0 LLM calls).
    - **Type Check Yes/No (`check_type`)**: Questions like `"Is this a PDF?"`, `"Is it a Word document?"` check the extension against requested type mapping (`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.txt`, `.csv`, `.py`, `.md`), returning `"Yes, this is a PDF file."` or `"No, this is a DOCX file."` (0 actions, 0 LLM calls).
    - **File Location (`get_location`)**: Queries like `"Where is it?"`, `"Where is this file?"`, `"What folder is it in?"`, `"Where did you open it from?"` return the parent directory containing the file with 0 actions and 0 LLM calls.
    - **Show/Reveal File Location (`reveal_location`)**: Commands like `"Show me where it is"`, `"Show me the file location"`, `"Open the folder containing it"`, `"Open its folder"`, `"Show this file in Explorer"` safely reveal the file in the OS file manager using the registered tool `reveal_file`:
      - Dispatches `explorer.exe /select,"<file_path>"` on Windows without `shell=True`, or `xdg-open <parent_dir>` on Linux.
      - Enforces strict safe root validation (`Desktop`, `Documents`, `Downloads`).
      - Validates that the file still exists and is a file before opening the location.
    - **Stale File Safety**: If the remembered file no longer exists or was moved/deleted, `reveal_file` requests safely report `"I remember that file, but it no longer exists at the previously known location."` with 0 execution actions, 0 LLM calls, and no automated filesystem rescanning.
    - **No-Context Safety**: When no file has been opened yet (`get_last_opened_file()` is `None`), all 4C.3 requests safely return `"I don't have a recently opened file to determine what \"it\" refers to. Please open or specify a file first."` with 0 actions and 0 LLM calls.
    - **Preserved Precedence & Regressions**: Pre-LLM deterministic intercept in `core/orchestrator.py` processes 4C.3 conversational file actions while maintaining full compatibility with 4C.1 (`Open it again`, `Open that file`), 4C.2 (`Open next`, `Open previous`), numeric selection (`Open 1`), and ordinal selection (`Open first`).

- **Milestone 4D.1**: Natural Language Application & Folder Control.
  - **Deterministic Application Launching**: Recognizes common natural-language verbs (`Open`, `Launch`, `Start`, `Run`) for allowed applications (`Chrome`, `Notepad`, `VS Code`, `Calculator`, `File Explorer`).
  - **Application Aliases**: Seamlessly normalizes aliases to their canonical application specifications (e.g. `Visual Studio Code` / `VS Code` / `Code` -> `Visual Studio Code`, `Calc` -> `Calculator`, `Google Chrome` -> `Google Chrome`, `File Explorer` -> `File Explorer`).
  - **Deterministic Safe Folder Opening (`open_folder`)**: Safely opens user folders (`Downloads`, `Documents`, `Desktop`) via natural phrasing (`Open Downloads`, `Open my Downloads folder`, `Open Documents`, `Open my Documents`, `Open Desktop`, `Show my Desktop folder`).
    - Registered as `open_folder` in `tools/registry.py` with `requires_approval=False`.
    - Executed in `tools/files.py` via `open_local_folder(folder_target)` and dispatched through `core/executor.py`.
    - Opens the folder using `os.startfile(str(target))` on Windows and `xdg-open` on Linux, strictly without `shell=True`.
    - Strict boundary enforcement: maps requests only to `SAFE_SEARCH_ROOTS` (`Desktop`, `Documents`, `Downloads`). Arbitrary paths like `C:\Windows\System32` are strictly blocked as `UNSAFE_LOCATION`.
  - **Folder vs. Application Disambiguation**: Commands like `Open Downloads` or `Open my Documents` are never misrouted to `open_application("downloads")` or treated as missing executables; they are routed directly to safe folder opening.
  - **Folder vs. Contextual File Action Disambiguation**: Contextual file location commands referring to the most recently opened file (`Open its folder`, `Open the folder containing it`, `Show me where it is`) preserve Milestone 4C.3 behavior (`reveal_file`) without conflicts.
  - **Browser Search Preservation**: Commands like `Search for Chrome extensions` and `Search for VS Code tutorials` remain web searches (`browser_search`) and never launch desktop applications.
  - **Unknown Application Rejection**: Unknown applications (`Open SomeRandomUnknownApp`) are cleanly validated and rejected via the explicit allowlist with `UNKNOWN_APPLICATION`, never guessing paths or hallucinating successful launches.
  - **Zero LLM Overhead**: All recognized application launches and folder openings are resolved deterministically before calling the LLM.

- **Milestone 4D.2**: Multi-Step Task Understanding & Ordered Execution.
  - **Conservative Multi-Step Command Detection**: Safely recognizes multi-action requests joined by sequential connectors (`and then`, `then`, `after that`, or `and`), such as `Open Chrome and search for Python tutorials`, `Open Downloads and then open Documents`, `Open Chrome and then open Calculator`.
  - **Search-Query Protection**: Protects search queries containing connective words (e.g. `Search for AI and machine learning tutorials`, `Search for Python and Java tutorials`) from accidental splitting, preserving them as single `browser_search` actions.
  - **Deterministic Pre-LLM Action Extraction**: When every segment in a connected request corresponds to a valid deterministic action (`open_application`, `open_folder`, `browser_search`), a multi-action `TaskPlan` is constructed deterministically without invoking the LLM.
  - **Maximum Action Limit**: Enforces a strict limit of at most 3 actions per request. Requests with $>3$ executable actions are safely paused with a clarification asking the user to split the request, preventing runaway execution.
  - **Strictly Sequential Execution**: Dispatches actions in the exact requested order (`Action 1` -> `Action 2` -> `Action 3`).
  - **Stop-on-Failure Behavior**: If any action in a multi-step plan fails during execution (e.g. `Open SomeRandomUnknownApp`), execution immediately halts, marking subsequent actions as unexecuted (`PREVIOUS_ACTION_FAILED`) and clearly reporting which action failed.
  - **Full Regression Safety**: Preserves all single-step actions (`Open Chrome`, `Open Downloads`, `Open my resume`, `Open the updated one`, `Open it again`, `Open next`, `Open previous`, `What is its name?`, `Where is it?`, `Show me where it is`). No `shell=True` and no arbitrary command execution.

- **Milestone 4D.3**: Fix Skipped Multi-Step Action Result Presentation.
  - **Explicit Action Execution State Separation**: Clearly distinguishes four distinct action execution states:
    1. **Completed**: `action_res.executed == True` and `action_res.success == True` (`✓ <tool> completed successfully`).
    2. **Failed**: `action_res.executed == True` and `action_res.success == False` (`✗ <tool> failed`).
    3. **Skipped**: `action_res.executed == False` and `action_res.error == "PREVIOUS_ACTION_FAILED"` (`⊘ <tool> skipped: Action was not executed because the previous action failed.`).
    4. **Unimplemented / Unsupported**: `action_res.executed == False` and `action_res.error == "EXECUTION_NOT_IMPLEMENTED"` (`- <tool>: Execution for '<tool>' is not implemented yet.`).
  - **Eliminated False "Not Implemented" Messages**: In `main.py`, skipped actions caused by a prior step failure (`PREVIOUS_ACTION_FAILED`) are presented explicitly with `⊘ <tool> skipped` and are excluded from the `[Execution for <tool> is not implemented yet.]` list.
  - **Preserved Safety & Behavioral Invariants**: Stop-on-failure remains strictly enforced for sequential multi-step tasks. Unknown applications continue to fail safely via allowlist rejection without shell execution or path guessing. All single-step and multi-step commands retain full backward compatibility.

- **Milestone 4D.4**: Safe Application Lifecycle Control.
  - **Purpose**: Implement safe, deterministic control over the lifecycle of supported applications, extending JARVIS to safely close applications (`Close Chrome`, `Quit Calculator`, `Exit VS Code`) and check their running status (`Is Chrome running?`, `Is Calculator open?`).
  - **Strict Allowlist-Based Lifecycle Control**:
    - Reuses the existing `APPLICATION_ALLOWLIST` in `tools/application.py` as the sole source of truth.
    - Each application entry specifies its canonical configuration, aliases, display name, registered process names (`process_names`), and closable policy (`closable: True | False`).
    - Arbitrary user process names (e.g. `Close explorer.exe`, `Close taskmgr`, `Close python`, `Close svchost`, `Close C:\Windows\System32\something.exe`) are strictly rejected and cannot reach process termination.
    - Unknown applications (`Close SomeRandomUnknownApp`) fail safely with `UNKNOWN_APPLICATION` without touching any process.
  - **Alias Normalization**:
    - Commands like `Close Code`, `Quit VS Code`, `Exit Visual Studio Code` all resolve to canonical `Visual Studio Code`.
    - Commands like `Close Chrome`, `Quit Chrome`, `Exit Google Chrome` resolve to canonical `Google Chrome`.
    - Commands like `Quit Calculator`, `Exit Calc` resolve to canonical `Calculator`.
  - **Protected Application Policy**:
    - Desktop shell applications such as `File Explorer` are explicitly marked with `closable: False`. Attempting to close them returns `APPLICATION_NOT_CLOSABLE` (`JARVIS is not allowed to close File Explorer.`), protecting desktop stability.
  - **No `shell=True` & Direct OS Process Control**:
    - Process discovery uses standard Windows Toolhelp32 snapshots via `ctypes` (`CreateToolhelp32Snapshot`, `Process32First`, `Process32Next`), enumerating process names directly in memory without shell execution.
    - Process termination uses standard library direct OS signals via `os.kill(pid, signal.SIGTERM)`. No shell strings, command substitutions, or raw `taskkill` subprocess executions with user input.
  - **Application Status Queries (`check_application_running`)**:
    - Recognizes queries like `Is Chrome running?`, `Is Calculator open?`, `Is VS Code currently running?`, `Is Chrome active?`.
    - Resolves application through canonical allowlist and inspects only registered process identities.
    - Returns factual responses: `"Yes, Google Chrome is currently running."` or `"No, Google Chrome is not currently running."`.
    - Executes directly as a non-destructive factual query without invoking the LLM.
  - **Multi-Step Integration**:
    - Fully integrates with Milestone 4D.2 and 4D.3: supports ordered combinations like `Open Chrome and then close Chrome`, `Open Calculator and then open Notepad and then close Calculator`.
    - Multi-step status checks: `Open Chrome and then check if Chrome is running`.
    - Stop-on-failure enforced: `Close SomeRandomUnknownApp and then open Calculator` safely halts, marking `open_application` as skipped (`PREVIOUS_ACTION_FAILED`) with Milestone 4D.3 presentation (`⊘ open_application skipped`).
  - **Intent Precedence & Protection**:
    - Browser searches like `Search for how to close Chrome` remain web searches (`browser_search`) and never trigger application termination.
    - Existing application launches (`Open Chrome`, `Open VS Code`) and folder commands continue to work without regression.
  - **Test Isolation & Mocks**:
    - 12 new dedicated unit tests in `tests/test_application_lifecycle.py` mock process enumeration and process termination to guarantee no real user processes are affected during automated runs.

- **Milestone 5.1**: Browser Automation Foundation & Page Awareness.
  - **Browser Automation Architecture & Session Management (`BrowserController` in `tools/browser.py`)**:
    - Created dedicated `BrowserController` class managing session state (`_active`, `_history`, `_history_index`), navigation (`open_website`), history navigation (`go_back`, `go_forward`), reload (`refresh`), and factual page awareness (`get_current_page_info`, `get_page_title`, `get_current_url`).
    - Dispatches real browser windows via Python standard library `webbrowser` controller (strictly `shell=False`) without requiring external binary webdriver installations.
    - Added singleton accessor `get_browser_controller()` with clean session reset capabilities (`reset()` / `reset_session()`).
  - **URL Sanitization & Safety Normalization (`validate_and_normalize_url`)**:
    - Enforces strict URL validation. Supported targets: explicit protocols (`https://`, `http://`), domain-like hosts (e.g. `example.com` normalized to `https://example.com`), and safe website aliases (`google` -> `https://www.google.com`, `youtube` -> `https://www.youtube.com`, `github` -> `https://github.com`, `chatgpt` -> `https://chatgpt.com`, `wikipedia` -> `https://www.wikipedia.org`).
    - Unconditionally rejects OS file paths, Windows drives (`C:\Windows...`), commands (`cmd.exe`, `powershell`, `python.exe`), shell characters, and standard file/document extensions (`.pdf`, `.docx`, `.py`, `.xlsx`, etc.) to prevent local file opening conflation.
  - **Deterministic Intent Extraction & Disambiguation (`ai/intent.py`)**:
    - Added `extract_browser_navigation_target(text)`: handles commands like `Open YouTube`, `Open Google`, `Go to example.com`, `Navigate to https://github.com`.
    - Added `extract_browser_control_action(text)`: handles `go back` (`browser_go_back`), `go forward` (`browser_go_forward`), `refresh` / `reload` (`browser_refresh`), and page queries (`What page am I on?`, `What is the title of this page?`, `What URL am I on?`).
    - **Website vs. Application Disambiguation**: Queries matching `APPLICATION_ALLOWLIST` (e.g. `Open Chrome`, `Open VS Code`) are routed strictly to `open_application`. Targets matching website aliases or valid domains (e.g. `Open Google`, `Open YouTube`, `Open example.com`) route to `browser_navigate`.
    - **Search Command Protection**: Web searches (e.g. `Search for how to go back in Chrome`, `Search for Python tutorials`) take strict precedence and route to `browser_search`, preventing false triggers of `browser_go_back`.
  - **Tool Registry, Executor & Planner Integration**:
    - Registered 5 new browser tools in `TOOL_POLICIES` (`browser_navigate`, `browser_go_back`, `browser_go_forward`, `browser_refresh`, `browser_get_page_info`) with `requires_approval=False`.
    - Added to `IMPLEMENTED_EXECUTION_TOOLS` in `core/executor.py` routing directly to `BrowserController`.
    - Added URL validation and sanitization in `validate_plan` (`core/planner.py`).
  - **Pre-LLM Deterministic Intercept & Multi-Step Support (`core/orchestrator.py`)**:
    - Intercepts single and multi-step browser actions deterministically without LLM hallucination.
    - Supports multi-step execution (e.g. `Open Google and then refresh the page`), sequential execution, max 3 actions per request, and stop-on-failure safety.
    - If no active browser session exists when a page-awareness query is issued, returns factual clarification (`"I don't currently have an active browser session to inspect."`) with 0 actions and 0 hallucination.
  - **Automated Verification**:
    - Created comprehensive test suite in `tests/test_browser_automation.py` (14 unit/integration tests).
    - Verified all 267 test cases across entire project suite pass with 100% success.
  - **Git Repository & Remote Synchronization**:
    - Initialized Git repository on branch `main`.
    - Configured `.gitignore` to protect virtual environments, cache files, and SQLite local database files (`data/*.db`, `storage/*.db`).
    - Added remote origin: `https://github.com/SaiAkhil-v1290/FRIDAY.git`.
    - Pushed project codebase (Milestones 1 through 5.1) to GitHub remote `origin/main`.

- **Milestone 4D.5**: Conversational Application Context & Pronoun Resolution.
  - **Purpose**: Enable natural conversational follow-up references (`it`, `that app`, `the app`, `that application`, `the application`, `this app`, `this application`) to the most recently successfully interacted-with approved application, mirroring Milestone 4C.3 conversational file context.
  - **WorkingMemory Application Context (`WorkingMemory`)**:
    - Stores the most recently interacted approved application via `_last_application` (`set_last_application`, `get_last_application`, `clear_last_application`).
    - Stored context format: `{"key": "<canonical_key>", "display_name": "<display_name>"}`.
    - Derived strictly from `APPLICATION_ALLOWLIST`. Arbitrary process names (e.g. `python.exe`, `svchost.exe`) are never saved into context.
    - Updated upon successful completion of `open_application` and preserved across `close_application` to permit immediate running-status follow-ups (e.g. `Close it` -> `Is it running?` -> `"No, Google Chrome is not currently running."`).
    - Failed or rejected actions (e.g. `Open SomeRandomUnknownApp`) never corrupt or overwrite existing valid application context.
  - **Deterministic Intent Extraction (`extract_conversational_application_action`)**:
    - Recognizes conversational close commands (`Close it`, `Quit it`, `Exit it`, `Close that app`, `Quit that app`, `Close the app`, `Exit the application`, `Close that application`).
    - Recognizes conversational status queries (`Is it running?`, `Is that app running?`, `Is the app open?`, `Is the application currently running?`, `Is it open?`, `Is it active?`, `Check if it is running`).
    - Excludes conversational file actions (e.g. `What is its name?`, `Where is it?`, `Show me where it is`, `Open its folder`), maintaining strict separation between file and application pronouns.
    - Excludes browser search queries (`Search for how to close Chrome`, `Search for why Chrome is running slowly`), preserving `browser_search` precedence.
  - **Pre-LLM Deterministic Routing & Precedence**:
    - Explicit application targets take strict precedence over conversational pronouns (e.g. if last app is Chrome, `Close Calculator` closes Calculator, not Chrome; `Is Chrome running?` checks Chrome, not VS Code).
    - Status queries resolve directly to `check_application_running` without invoking the LLM.
    - Close commands resolve directly to `close_application` with the canonical display name, never passing the literal pronoun string `"it"`.
  - **No-Context Safety**:
    - When no application has been referenced or remembered (`get_last_application()` is `None`), commands like `Close it` or `Is it running?` safely return a clarification prompt (`"I don't have a recently used application to determine what \"it\" refers to. Please specify the application."`) with 0 computer actions, 0 process inspections, and 0 LLM calls.
  - **Multi-Step Integration & Context Resolution**:
    - Multi-step chains seamlessly support conversational pronouns (e.g. `Open Chrome and then close it`).
    - Within multi-step commands (e.g. `Open Chrome and then open Calculator and then close it`), conversational pronouns deterministically resolve to the most recently explicitly referenced valid application in that sequence (`Calculator`).
    - Multi-step failure safety: If a preceding step fails (e.g. `Open SomeRandomUnknownApp and then close it`), execution immediately halts under stop-on-failure (`PREVIOUS_ACTION_FAILED`), and the unexecuted close step is presented with `⊘ close_application skipped` without targeting any stale application.
  - **Protected Application Policy**:
    - Protected applications (e.g. `File Explorer` with `closable: False`) remain strictly protected when targeted via pronouns (`Open File Explorer` -> `Close it` returns `"JARVIS is not allowed to close File Explorer."`).
  - **Verification & Test Coverage**:
    - 14 dedicated unit tests in `tests/test_conversational_application.py` verify all pronoun variations, no-context safety, failed action safety, multi-step resolution, and file/search regressions.
    - Full project suite passes: 253 passed in 56.24s.

## Execution Architecture (Milestones 3, 4A, 4B & 4C)
```text
Validated TaskPlan
        ↓
PlanExecutor (`core/executor.py`)
        ↓
Policy Checks:
  1. Is tool registered? (Must be in `tools/registry.py`)
  2. Does tool require approval? (If `requires_approval=True`, held without explicit user approval)
  3. Is tool execution implemented? (`open_application`, `browser_search`, `find_files`, `search_file_contents`, and `open_file`)
        ↓
Implemented Dispatchers:
  • `open_application` (`tools/application.py`):
      - Normalizes app name
      - Resolves through strict deterministic allowlist (`APPLICATION_ALLOWLIST`)
      - Spawns detached processes (`subprocess.Popen` with `shell=False`)
  • `browser_search` (`tools/browser.py`):
      - Rejects empty, whitespace, or oversized queries (> 500 chars)
      - Safely URL-encodes queries via `urllib.parse.quote_plus`
      - Treats query strictly as search text data (prevents command injection)
      - Dispatches via `webbrowser.open()` without shell command strings
  • `find_files` (`tools/files.py`):
      - Read-only file discovery within explicit safe roots (Desktop, Documents, Downloads)
      - Validates paths using Path.resolve() and containment checks (prevents path traversal / symlink escapes)
      - Matches exact/partial filenames, common extensions, and directory listings
      - Bounded by MAX_RESULTS (100) and graceful exception isolation for permission errors
      - Never modifies, moves, deletes, writes, or executes files; never invokes a shell
  • `search_file_contents` (`tools/files.py`):
      - Read-only literal text content searching inside supported text-based files & documents
      - Line-by-line streaming inspection; skips files > 5MB to protect memory
      - Returns matching file paths, line numbers, and bounded contextual snippets
      - Bounded by MAX_CONTENT_MATCHES (50) and enforces safe roots & directory exclusions
  • `open_file` (`tools/files.py`):
      - Safely opens verified local files inside SAFE_SEARCH_ROOTS via os.startfile / non-shell xdg-open
      - Validates path containment, rejects directories and non-existent targets
      - Deterministic ambiguity guard: never guesses among multiple matching candidates, always prompts with numbered list
      - Supports contextual index selection (1st result), pronouns (it, that PDF), and explicit filenames
        ↓
ActionExecutionResult & PlanExecutionResult returned to orchestrator and displayed in `main.py`
```

## Memory Architecture
```text
WORKING MEMORY (In-Memory / RAM)
    - Transient session conversation history
    - Managed via `WorkingMemory` in `memory/working_memory.py`
    - Reset when the process exits

LONG-TERM MEMORY (Local SQLite)
    - Stored in `data/jarvis.db`
    - Managed via `LongTermMemory` in `memory/long_term_memory.py` & `memory/database.py`
    - Tables: `memories`, `profile`, `preferences`
    - Handled via `MemoryCommandHandler` in `memory/commands.py` for explicit commands:
        • memory_store: "remember that ...", "save this: ..."
        • memory_forget: "forget that ...", protects against bulk deletions ("forget everything")
        • memory_recall: "what do you remember about me / my projects / preferences?"
    - Retrieval Layer: `MemoryRetriever` in `memory/retrieval.py`
        • Normalizes request tokens (filters stop words)
        • Scores candidate memories deterministically
        • Enforces strict limits (`MAX_RETRIEVED_MEMORIES = 5`, `MAX_MEMORY_CHARS = 1200`)
    - Grounding & Anti-Hallucination: `MemoryGrounder` in `memory/grounding.py`
        • Identifies memory-dependent factual queries (`is_memory_fact_query`)
        • Detects missing requested aspects (`detect_missing_aspect`), injecting explicit uncertainty notices
        • Permits improvement suggestions (`is_suggestion_query`) but mandates clear separation from known facts
        • Strictly prohibits inventing project status (e.g. "planning stage") or unmentioned tech stacks
```

## Recent Changes
- **Milestone 4B.6.1 Bug Fix: Application Opening, Natural Filename Matching & Large Result Ambiguity**:
  - **Bug 1 Fix (Application Routing & File Safety Separation)**: In [`core/orchestrator.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/orchestrator.py), explicit application launching commands (e.g. `"Open Notepad"`) enforce `raw_plan.intent = "open_application"` and deterministic response formatting (`"Opening <app>."`), preventing hallucinated or leaked file safety error messages (`"outside the allowed locations"`) from being displayed when an allowlisted application is executed. Application targets are resolved purely via `APPLICATION_ALLOWLIST` and never pass through file safe-root checks, while unsafe system paths (e.g. `Open C:\Windows\System32\cmd.exe`) remain strictly rejected.
  - **Bug 2 Fix (Natural Filename Matching & No-Guess Policy)**: In [`core/orchestrator.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/orchestrator.py), natural filename queries such as `"Open my resume"` are normalized by removing conversational filler words (`"my"`, `"the"`, `"file"`, `"document"`) and extracting key search terms (`"resume"`). Matching handles case insensitivity and separators (`_`, `-`, spaces, parentheses). Exact filename matches retain highest priority. If multiple candidates match (e.g., `Akhil_Resume(Updated).pdf`, `MyResume.pdf`, `Resume_Akhil.pdf`), JARVIS strictly refuses to guess and prompts for clarification with a numbered list. A single match opens automatically, and non-matching requests safely return not found.
  - **Bug 3 Fix (Large Ambiguity UX & Preserved Selection Indexing)**: In [`core/orchestrator.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/orchestrator.py) and [`memory/working_memory.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/memory/working_memory.py), when a pronoun reference (e.g. `"Open it"`) resolves to a large result set (> 10 items), JARVIS avoids dumping dozens of items and instead shows a bounded preview (first 10 items) along with total count and usage examples (`"Open result 14"`). Working memory stores up to 100 recent file items with full paths and total search count, ensuring queries like `"Open result 14"` reliably open item #14 even if only the first 10 items were displayed in chat. Small result sets ($\le 10$) continue to display the standard numbered list.
  - **Regression Tests**: Added Tests 74–82 in [`tests/test_file_discovery.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/tests/test_file_discovery.py), bringing total passing tests across the project to 151.
- **Milestone 4B.6 Safe File Opening & Contextual File Selection**:
  - **Tool & Core Function (`open_local_file`)**: Added `open_local_file` in [`tools/files.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/tools/files.py) returning `FileOpenResult`. Validates containment inside `SAFE_SEARCH_ROOTS` (`Desktop`, `Documents`, `Downloads`), ensures target exists and is a file (rejects directories), and launches without shell commands via `os.startfile` on Windows or `xdg-open` with `shell=False`. Handled `PermissionError` cleanly.
  - **Contextual File Working Memory**: In [`memory/working_memory.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/memory/working_memory.py), added bounded storage (`remember_file_results` / `get_last_file_results`) for recent file discovery and content search outputs, storing up to 50 results with name, path, and extension.
  - **Deterministic Ambiguity Guard & No-Guess Policy**:
    - **Single File Follow-up**: Pronoun requests (`"Open it"`, `"Can you open it?"`, `"Open that PDF"`) with a single candidate safely create an `open_file` plan.
    - **Multiple Matches Clarification**: When multiple files match a request, JARVIS strictly refrains from guessing and presents a numbered clarification prompt.
    - **Numbered Index Selection**: Supported ordinal/cardinal selection (`"Open the first result"`, `"Open result 2"`, `"Open the second one"`). Invalid indices (e.g. index 10 when 3 exist) fail with an informative clarification rather than guessing.
    - **Explicit Filename Resolution**: Explicit filenames (`"Open my resume"`, `"Open Akhil_Resume(Updated).pdf"`) match against recent results or fallback to safe-root file discovery. Single match -> `open_file`; multiple matches -> clarification prompt; not found -> clear notice.
  - **Path Traversal & Unsafe Path Defense**: Direct paths outside safe roots (e.g., `C:\Windows\System32\something.exe`, `../../Windows/file.txt`) are rejected with `UNSAFE_LOCATION`.
  - **Planner & Executor Integration**: Added `open_file` to `TOOL_POLICIES` (`requires_approval=False`), `IMPLEMENTED_EXECUTION_TOOLS`, and plan validator (exempted `open_file` from folder path collapsing).
  - **Comprehensive Test Suite**: Added 14 unit and integration tests (Tests 60–73 in `tests/test_file_discovery.py`), bringing total passing tests to 142.
- **Milestone 4B.5.1 Bug Fix: Local Document Search Routing & Follow-Up File Context**:
  - **Local Document vs Browser Search Precedence**: Corrected intent routing in `ai/intent.py` and `core/orchestrator.py` so requests with strong local indicators (`my presentations`, `my PowerPoint files`, `my slides`, `my PDFs`, `my documents`, `inside my PDFs`, `in Downloads`, etc.) deterministically route to local `search_file_contents` rather than `browser_search`.
  - **Explicit Web Search Indicators**: Preserved browser search priority when explicit web indicators are present (`Google`, `the web`, `online`, `Bing`, `internet`), avoiding broad fallback on the standalone word `search`.
  - **Follow-Up File Search Context**: Added `get_last_file_search()` in `memory/working_memory.py` and `extract_followup_file_search_clarification()` in `ai/intent.py`. Immediate follow-up clarifications (e.g., `"Its name is Akhil_Resume(Updated) can you find it"`) inherit safe location context (`folder="Downloads"`) and trigger a targeted `find_files` action without hallucinated actions or conversation fallbacks.
  - **Content Search vs Discovery Clarification Transition**: Allowed follow-up name clarifications to smoothly pivot from exploratory content searches to precise file discovery with inherited extension filters.
- **Milestone 4B.5 Safe Document Content Search**:
  - **Document Extractors**: Integrated lightweight, read-only extraction for rich documents:
    - PDF (`.pdf`) via `pypdf.PdfReader` with page-by-page streaming.
    - Word (`.docx`) via `python-docx` with paragraph-by-paragraph and table inspection.
    - PowerPoint (`.pptx`) via `python-pptx` with slide-by-slide text frame traversal.
    - Excel (`.xlsx`) via `openpyxl.load_workbook(..., read_only=True, data_only=True)` with sheet and cell-coordinate extraction, preventing formula execution.
  - **Structured Location Metadata**: Generalized `ContentMatch` to report natural locations (`Page N` for PDFs, `Paragraph N` or `Table N, Row M` for Word, `Slide N` for PowerPoint, `Sheet "<name>", Cell <coord>` for Excel, and `Line N` with backward-compatible `line_number` for text files).
  - **Safety Boundaries**: Strictly read-only, non-macro-executing, and isolated within `SAFE_SEARCH_ROOTS` (`Desktop`, `Documents`, `Downloads`). Pruned excluded dirs (`.git`, `.venv`, `node_modules`, etc.), skipped files exceeding 5 MB (`MAX_CONTENT_FILE_SIZE`), and handled malformed or corrupted documents gracefully without crashes.
  - **Deterministic Intent & Precedence**: Added natural language patterns for document content search (`"Which PDF contains..."`, `"Search my Excel files for revenue"`, `"Find Word documents mentioning..."`, `"Search presentations for..."`). Preserved web search precedence (`"Search Google for PDFs about Docker"` routes to `browser_search`) and dangerous task protection (`"delete all PDFs in Downloads"` requires approval).
- **Milestone 4B.4 Safe Local File Content Search**:
  - **Tool & Core Function (`search_file_contents`)**: Implemented safe literal text scanning inside supported text files (`.txt`, `.py`, `.md`, `.json`, `.csv`, `.html`, `.css`, `.js`, `.ts`, `.yaml`, `.yml`, `.xml`, `.ini`, `.cfg`, `.log`, `.rtf`).
  - **Memory & Resource Safeguards**: Bounded by `MAX_CONTENT_FILE_SIZE = 5 MB` (skips larger files), `MAX_SNIPPET_LENGTH = 150`, and `MAX_CONTENT_MATCHES = 50`. Streamed line-by-line using `utf-8` with `errors='ignore'` without loading entire files into memory.
  - **Contextual Snippets**: Returns structured `ContentMatch` objects with filename, full path, line number, and a bounded line snippet centered around the match.
  - **Intent & Priority Routing**: In `ai/intent.py`, added deterministic extraction for natural content-search phrases (`which file contains...`, `find documents mentioning...`, `search inside my text files for...`, `find files containing...`). Browser searches (`search Google for Python files`) strictly take precedence.
  - **Planner & Executor Integration**: Added `search_file_contents` to `TOOL_POLICIES` and `IMPLEMENTED_EXECUTION_TOOLS`. Validated that query is non-empty and formatted readable execution results with snippets.
- **Milestone 4B.3 Smart File Metadata, Filtering & Sorting**:
  - **Lightweight Metadata & Human-Readable Sizing**: Added `size` (bytes), `modified_time` (epoch float), `human_size` (formatted to `B`, `KB`, `MB`, `GB`), and `formatted_modified_time` (`YYYY-MM-DD HH:MM`) to `FileInfo` without opening files or computing recursive directory sizes.
  - **Deterministic File Type Filtering**: Extended `COMMON_FILE_EXTENSIONS` to support multi-extension categories deterministically: Images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.svg`), Videos (`.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`), Documents (`.pdf`, `.doc`, `.docx`, `.txt`, `.rtf`, `.odt`), plus individual types (`.py`, `.pdf`, `.txt`, etc.).
  - **Deterministic Size Filtering**: Parsed natural-language size constraints (`larger than`, `smaller than`, `over`, `under`, `less than`) and units (`B`, `KB`, `MB`, `GB`) into byte comparisons without `eval()`.
  - **Deterministic Time Filtering**: Handled `today` (midnight to current local time), `yesterday` (previous day midnight to today midnight), and `this_week` (Monday midnight of current local week to current time).
  - **Sorting & Ranking**: Implemented sorting by `modified_time` (`newest` = desc, `oldest` = asc) and `size` (`largest` = desc, `smallest` = asc). When metadata sorting is not requested, preserved deterministic exact/partial filename ranking.
  - **Combined Query Support**: Combined location, file type/extension, time, size, and sorting seamlessly (e.g. `"find the newest PDF files in Downloads larger than 5 MB"`).
  - **Execution Formatting**: Enhanced output in [`core/executor.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/executor.py) to display `Path`, `Size`, and `Modified` when metadata filtering/sorting is applied while keeping simple searches clean and concise.
- **Milestone 4B.2 Smart Directory Listing & Navigation**:
  - **Differentiated Search vs. Listing Modes**:
    - **Search mode** (`recursive=True`): For requests like `"find report.pdf"`, `"find Python files"`, or `"find PDF files in Downloads"`, performs recursive traversal bounded within safe roots while pruning excluded development folders.
    - **Listing mode** (`recursive=False`): For location-only requests like `"what files are on my Desktop"`, `"list files in Downloads"`, or `"show me files in Documents"`, inspects only the immediate top-level items of the target directory using `os.scandir()`. Never traverses nested subdirectories.
  - **Structured Result Separation**: Separated immediate subdirectories into `directories` and files into `files` on `FileSearchResult`, capturing lightweight metadata (`size` for files without recursive folder size calculations).
  - **Deterministic Intent Extraction**: In `extract_explicit_file_search_params()`, recognized listing patterns (e.g., `"what files are on my Desktop"`, `"show what's in Downloads"`) and tagged them with `mode="list"`, which sets `recursive=False` on the resulting `find_files` action.
  - **User-Facing Display**: In [`core/executor.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/executor.py), directory listing output formats into distinct `Folders (N):` and `Files (N):` lists without dumping nested project assets.
  - **Preserved Safety Boundaries**: Directory listings remain restricted strictly to `SAFE_SEARCH_ROOTS` (`Desktop`, `Documents`, `Downloads`), rejecting system paths like `C:\Windows` with `UNSAFE_LOCATION`.
- **Milestone 4B.1 Safe Local File Discovery Usability & Quality Refinements**:
  - **Exact Filename & Space Preservation (Bugs 1 & 2 Fix)**: In [`ai/intent.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/ai/intent.py) and [`core/planner.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/planner.py), exact filename queries (e.g. `"find report.pdf"`, `"find Homemade Indian Food Menu.png"`, `"find Homemade Indian Food"`) are fully preserved with spaces and punctuation intact, generating `find_files` actions deterministically. In [`core/planner.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/core/planner.py), semantic validation was updated to accept explicit file discovery requests directly even when natural language lacks generic keywords like "files" or "folder".
  - **Filename + Location Without "Find"**: Added deterministic pattern matching in `extract_explicit_file_search_params` for `<filename>.<ext> in/on <safe_root>` (e.g. `"Homemade Indian Food Menu.png in Downloads"`, `"notes.txt on Desktop"`), resolving query and location while ignoring ordinary conversational text (e.g. `"I have a report.pdf for college."`).
  - **Location-Only File Listing (Bug 3 Fix)**: Added pattern recognition for location-only listings (e.g. `"show me files in Documents"`, `"list files in Downloads"`, `"what files are on my Desktop"`), generating `find_files` with `location=<safe_root>` and empty query, bounded by `MAX_RESULTS = 100`.
  - **Dependency & Cache Exclusion (Bug 4 Fix)**: Added `DEFAULT_EXCLUDED_DIRS` (`.git`, `.venv`, `venv`, `__pycache__`, `node_modules`, `site-packages`, `dist`, `build`, etc.) to [`tools/files.py`](file:///c:/Users/saiak/OneDrive/Desktop/Javy/tools/files.py), pruning excluded directories during `os.walk(..., followlinks=False)` traversal.
  - **Deterministic Result Ranking**: Exact filename matches appear first, followed by partial matches.
- **Milestone 4B Safe Read-Only File Discovery**:
  - **`tools/files.py`**: Created deterministic read-only file discovery tool (`discover_files`, `FileInfo`, `FileSearchResult`). Only searches within explicit `SAFE_SEARCH_ROOTS` (`Desktop`, `Documents`, `Downloads`). Fully rejects path traversal attempts (`../../Windows/System32`) and system directories (`C:\Windows\System32`) using `Path.resolve()` containment checks. Bounded by `MAX_RESULTS = 100` and ignores symlinks pointing outside roots. Strictly read-only: no writing, modification, execution, or shell invocations.
  - **`core/executor.py`**: Added `find_files` to `IMPLEMENTED_EXECUTION_TOOLS` with argument routing (`query`, `extension`, `folder`). Formats structured file discovery outputs cleanly.
  - **`ai/intent.py`**: Added deterministic parameter extraction for file discovery (`extract_explicit_file_search_params`) supporting queries, extensions, and target folders while strictly preserving `missing_input` for `"open this file"` and `dangerous_task` for file deletions.
  - **`core/orchestrator.py`**: Integrated deterministic file discovery plan generation (`ProposedAction(tool="find_files", ...)`).
  - **`main.py`**: Updated output display for executed `find_files` actions.
  - **`tests/test_file_discovery.py`**: Created comprehensive 15-test suite covering exact/partial search, extension filtering, safe listing, traversal/system rejection, shell query safety, result limits, error resilience, executor integration, and regression checks.
- **Milestone 4A Browser Search Bug Fixes & Refinements**:
  - **Bug 1 Fix (Duplicate Chrome Launch Prevention)**: In `ai/intent.py` (`extract_explicit_open_app_target`) and `core/orchestrator.py`, combined commands like `"open Chrome and search for ..."` or `"open browser and search for ..."` are recognized as search commands and executed strictly as a single `browser_search` action. Opening Chrome separately via `open_application` is bypassed, avoiding opening duplicate windows or tabs.
  - **Bug 2 & Bug 3 Fix (Query Integrity & Shell-like Safety)**: In `ai/llm.py`, removed over-aggressive query truncation regex (`_sanitize_arguments_against_contamination`) that previously split on `&`, `+`, or connective words. In `core/orchestrator.py`, deterministic extraction preserves literal query strings (including `&`, `+`, quotes, and shell-like characters such as `'; rm -rf /'`), while `tools/browser.py` properly URL-encodes the query with `urllib.parse.quote_plus` and opens it safely via Python's standard `webbrowser` without any shell invocation.
  - **Bug 4 Fix (Web Search Priority Over Dangerous Keywords)**: In `ai/intent.py` (`classify_intent`) and `core/planner.py` (`validate_plan`), search requests (e.g. `"open Chrome and search for how to delete all files"`, `"google format C drive meaning"`) are recognized as web search tasks before local dangerous task pattern matching. Search queries are treated strictly as data without triggering local file deletion policies or approval gates, while actual local deletion tasks (e.g. `"delete all files in my Downloads folder"`) continue to be strictly classified as `dangerous_task` requiring explicit approval.
- **Missing Input & Ambiguous Task Guarding**:
  - **Bug 1 Fix (`ai/intent.py` & `core/orchestrator.py`)**: Added `missing_input` intent classification (`check_missing_input()`). Commands requiring essential inputs that were omitted (such as `"run this Python code"`, `"run this code"`, `"open this file"`) deterministically request the required content/file with `actions = []`, eliminating fabricated actions (e.g. opening VS Code or creating dummy files).
  - **Bug 2 Fix (`ai/intent.py`)**: Hardened `extract_explicit_open_app_target()` to reject action gerunds and non-application targets (`deleting`, `removing`, `cleaning`, `organizing`, `code`, `files`). Categorized ambiguous deletion/cleaning requests (e.g. `"start deleting old files"`, `"start cleaning my Downloads folder"`) under `unclear` to prompt for specific files and locations without generating `open_application` or attempting execution.
- **Post-Milestone 3 Bug Fixes & Security Hardening**:
  - **Bug 1 Fix (`ai/intent.py`)**: Enhanced intent regex to classify all `open/launch/start/run <application>` variations as `task` regardless of case or target, while excluding non-application phrases (`open to suggestions`, `open minded`).
  - **Bug 2 Fix (`core/orchestrator.py` & `tools/application.py`)**: Added deterministic fallback for explicit application commands so LLM conversational refusals cannot bypass the allowlist resolver. Unknown apps (`open SomeRandomUnknownApp`) reach the executor and are cleanly rejected as not allowed.
  - **Bug 3 Fix (`ai/intent.py` & `core/planner.py`)**: Prioritized prompt injection defense so adversarial override requests are blocked immediately with safe messaging and `actions = []`. Strict semantic checks in `core/planner.py` prevent hallucinated destructive actions (e.g. `delete_files`).
  - **Security Hardening (`tools/application.py`)**: Removed shell/terminal binaries (`cmd`, `powershell`, `terminal`) from `APPLICATION_ALLOWLIST`. Only safe user desktop applications are executable.
- **`memory/grounding.py`**: Created `MemoryGrounder` to detect factual queries about user projects/preferences, check for missing technical aspects (e.g. database, framework), format anti-hallucination grounding rules, and mandate separation of known facts from suggestions.
- **`ai/conversation.py`**: Updated conversational prompt with `MEMORY GROUNDING RULES`, instructing the model to treat retrieved memories as the sole factual source of truth and explicitly state uncertainty when information is not present. Lowered temperature to `0.3` for factual stability.
- **`core/orchestrator.py`**: Integrated `MemoryGrounder` alongside `MemoryRetriever`.
- **`tests/test_memory_grounding.py`**: Added unit tests covering known fact grounding, missing fact uncertainty detection, suggestion vs. fact separation, status hallucination prevention, and regressions.

- **Milestone 5.1.1 & 5.1.1.2**: Local Audio I/O, Voice Activity Detection Foundation & Audio Lifecycle Hardening:
  - **Git Branch Isolation**: Phase 5 work is strictly isolated on branch `akhil/phase-5-voice` branching from `origin/main`. Preserves `akhil/phase-3-computer-control` intact and unmerged without carryover of unrelated Phase 3 changes.
  - **Dedicated Voice Package (`voice/`)**:
    - `voice/config.py`: Centralized audio configuration (`VoiceConfig`) with strict validation for sample rates (8k, 16k, 32k, 48k), mono channels, 16-bit PCM, frame durations (10, 20, 30 ms), VAD aggressiveness mode (0-3), silence timeout (`vad_silence_duration_ms`), minimum speech duration (`vad_min_speech_duration_ms`), configurable energy thresholds (`silence_rms_threshold`, `speech_rms_threshold`), maximum pre-speech buffer duration (`max_pre_speech_duration_ms`), and bounded total utterance limits (`max_buffer_duration_sec`).
    - `voice/audio_io.py`:
      - `AudioInputSource` & `AudioOutputSink`: Decoupled abstract interfaces for audio I/O with concrete `SoundDeviceInputSource` and `SoundDeviceOutputSink` backends wrapping `sounddevice`.
      - `SoundDeviceOutputSink`: Hardened `stop()` to immediately abort and close streams, releasing resources and resetting `_stream = None` so subsequent playback cleanly reopens fresh hardware streams.
      - `AudioPlaybackService`:
        - Non-blocking asynchronous playback with immediate thread-safe cancellation (`stop()`, barge-in support).
        - Deadlock-free blocking playback: releases `_playback_lock` during blocking write iterations while keeping thread-safe stop event signaling.
        - Worker overlap protection: joins terminating workers with timeout and strictly rejects starting overlapping playback workers if an unresponsive worker fails to exit.
        - Fresh stream reuse: supports immediate successive playback calls after cancellation.
      - `AudioCaptureService`:
        - Dedicated background capture worker with volatile bounded queue (`max_queue_size`).
        - Hardened shutdown sequence: marks stop event, explicitly closes hardware input source first to unblock blocking PortAudio driver reads, and joins worker thread safely without abandoned threads.
        - Safe startup: rejects starting if an alive worker from a previous session is still terminating, while remaining idempotent if already running.
    - `voice/vad.py`:
      - `VoiceActivityDetector`: State machine for speech onset and speech termination detection using `webrtcvad` with deterministic RMS energy thresholding fallback.
      - Bounded pre-speech buffer: caps pre-speech onset frames to `max_pre_speech_bytes` (`max_pre_speech_duration_ms`).
      - Bounded utterance buffer: enforces `max_buffer_bytes` (`max_buffer_duration_sec`), preventing unbounded RAM usage during sustained background noise.
      - Explicit truncation reporting: sets `is_truncated = True`, emits `buffer_overflow` events immediately upon exceeding buffer capacity, tags frame events with `truncated=True`, and preserves truncation status in the final `speech_end` event payload.
      - Configurable energy thresholds: uses `silence_rms_threshold` (default 150.0) and `speech_rms_threshold` (default 450.0).
      - Clean state reset: `reset()` clears speech buffers, pre-speech buffers, frame counters, and truncation flags.
  - **Testing & Verification**:
    - `tests/test_voice_audio_io.py`: 28 unit and integration tests covering capture lifecycle, playback, cancellation, blocking playback interruption without deadlock, reuse after cancellation, worker overlap prevention, unblocking capture shutdown, buffer overflows, and missing device handling.
    - `tests/test_voice_vad.py`: 13 unit and integration tests covering speech detection, silence accumulation, speech start/end events, frame validation, pre-speech buffer bounding, utterance overflow events, truncation reporting, and state resets.
    - 100% headless mock testing without physical microphone or speaker hardware dependencies.
    - Total test suite: 308 tests passing across the entire project.

## Execution
1. Activate venv: `.\.venv\Scripts\Activate.ps1`
2. Ensure model is pulled: `ollama pull qwen2.5:3b`
3. Run `python main.py`

