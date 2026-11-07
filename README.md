# JARVIS — Milestone 1: Local Brain

A local-first Windows assistant foundation. This milestone connects to Ollama,
converts natural-language requests into validated structured plans, and prints
those plans. It does **not** execute computer actions.

## Requirements

- Windows 10/11
- Python 3.12 recommended
- Ollama for Windows
- A model supported by your machine (default: `qwen2.5:3b`)

## Setup (PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# If activation is blocked in this PowerShell session:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
Copy-Item .env.example .env

ollama pull qwen2.5:3b
```

Make sure Ollama is running. The Windows Ollama app normally runs in the
background. If needed, start the server in a separate terminal:

```powershell
ollama serve
```

Then, from the project folder with the virtual environment active:

```powershell
python main.py
```

## Example requests

- `Open Chrome and search for local-first AI agents`
- `Find my resume in Downloads`
- `Draft an email asking for an assignment extension`
- `Read my screen and explain this error`

The assistant will propose a plan. It will not perform the actions.

## Configuration

Edit `.env`:

```env
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_TIMEOUT=120
MAX_HISTORY_MESSAGES=12
```

You can change the model to another model already pulled in Ollama.

## Safety boundaries in this milestone

- No shell tool.
- No arbitrary Python execution.
- No screen capture.
- No browser control.
- No file access.
- No email sending or purchases.
- Model output is validated using Pydantic.
- Proposed tool names must exist in the local registry.
- Tool approval policies are enforced outside the model.

## Current limitations

- Working memory exists only in RAM and is discarded on exit.
- The tool registry is descriptive; tools are not executable yet.
- This is a terminal test harness, not the background voice application.

## Next milestone

Implement SQLite-backed personal memory, task history, and a controlled
memory manager. Keep credentials in Windows Credential Manager, not SQLite.
