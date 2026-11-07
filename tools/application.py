import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ApplicationLaunchResult:
    success: bool
    message: str
    application: str
    resolved_target: str | None = None
    error: str | None = None


@dataclass
class ApplicationCloseResult:
    success: bool
    message: str
    application: str
    terminated_count: int = 0
    error: str | None = None


@dataclass
class ApplicationStatusResult:
    success: bool
    application: str
    running: bool
    process_count: int = 0
    message: str = ""
    error: str | None = None


# Explicit deterministic allowlist mapping normalized names to launch & lifecycle specifications.
# Hardened: Terminal and shell binaries (cmd, powershell, wt/terminal) are strictly removed.
# Only safe desktop user applications are permitted.
APPLICATION_ALLOWLIST: dict[str, dict[str, object]] = {
    # Browsers
    "chrome": {
        "binary": "chrome.exe",
        "default_paths": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ],
        "display_name": "Google Chrome",
        "process_names": ["chrome.exe"],
        "closable": True,
    },
    "google chrome": {
        "alias_of": "chrome",
    },
    "msedge": {
        "binary": "msedge.exe",
        "default_paths": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
        "display_name": "Microsoft Edge",
        "process_names": ["msedge.exe"],
        "closable": True,
    },
    "edge": {
        "alias_of": "msedge",
    },
    "microsoft edge": {
        "alias_of": "msedge",
    },

    # Editors & IDEs
    "notepad": {
        "binary": "notepad.exe",
        "default_paths": [
            r"C:\Windows\System32\notepad.exe",
            r"C:\Windows\notepad.exe",
        ],
        "display_name": "Notepad",
        "process_names": ["notepad.exe"],
        "closable": True,
    },
    "vscode": {
        "binary": "code.cmd",
        "default_paths": [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"),
            r"C:\Program Files\Microsoft VS Code\Code.exe",
            r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
        ],
        "display_name": "Visual Studio Code",
        "process_names": ["code.exe"],
        "closable": True,
    },
    "vs code": {
        "alias_of": "vscode",
    },
    "visual studio code": {
        "alias_of": "vscode",
    },
    "code": {
        "alias_of": "vscode",
    },

    # Utilities
    "calculator": {
        "binary": "calc.exe",
        "default_paths": [
            r"C:\Windows\System32\calc.exe",
        ],
        "display_name": "Calculator",
        "process_names": ["calc.exe", "calculatorapp.exe", "calculator.exe"],
        "closable": True,
    },
    "calc": {
        "alias_of": "calculator",
    },
    "explorer": {
        "binary": "explorer.exe",
        "default_paths": [
            r"C:\Windows\explorer.exe",
        ],
        "display_name": "File Explorer",
        "process_names": ["explorer.exe"],
        "closable": False,
    },
    "file explorer": {
        "alias_of": "explorer",
    },
}


def normalize_app_name(raw_name: str) -> str:
    """Normalize application name by stripping quotes, punctuation, and extra whitespace."""
    if not raw_name:
        return ""
    cleaned = raw_name.strip().strip("'\"").lower()
    return " ".join(cleaned.split())


def resolve_application(raw_name: str) -> tuple[str, dict] | None:
    """
    Resolve requested name through explicit allowlist.
    Returns (canonical_name, spec) or None.
    Rejects numeric and indexed selection targets unconditionally.
    """
    normalized = normalize_app_name(raw_name)
    if not normalized:
        return None

    # Never treat numeric or index-like targets as applications
    if normalized.isdigit() or normalized.startswith("#"):
        return None

    entry = APPLICATION_ALLOWLIST.get(normalized)
    if not entry:
        return None

    # Resolve alias if present
    if "alias_of" in entry:
        canonical_name = entry["alias_of"]
        entry = APPLICATION_ALLOWLIST.get(canonical_name)
        return (canonical_name, entry) if entry else None

    return normalized, entry


def launch_application(raw_name: str) -> ApplicationLaunchResult:
    """
    Safely launch an application from the deterministic allowlist.
    Never executes shell=True or arbitrary user-supplied executable paths.
    """
    resolved = resolve_application(raw_name)
    if not resolved:
        return ApplicationLaunchResult(
            success=False,
            message=f"'{raw_name}' is not in the allowed application list.",
            application=raw_name,
            error="UNKNOWN_APPLICATION",
        )

    canonical_name, spec = resolved
    binary_name = spec.get("binary")
    display_name = spec.get("display_name", canonical_name)
    default_paths = spec.get("default_paths", [])

    # Find valid executable path on system
    target_executable = None

    # 1. Check explicit default Windows paths
    for path_str in default_paths:
        if path_str and Path(path_str).is_file():
            target_executable = path_str
            break

    # 2. Check system PATH via shutil.which
    if not target_executable and binary_name:
        path_which = shutil.which(binary_name)
        if path_which and Path(path_which).is_file():
            target_executable = path_which

    if not target_executable:
        return ApplicationLaunchResult(
            success=False,
            message=f"Could not find executable for '{display_name}' on this machine.",
            application=display_name,
            error="EXECUTABLE_NOT_FOUND",
        )

    # Launch without shell=True, detached in Windows
    try:
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(
            [target_executable],
            creationflags=flags,
            close_fds=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ApplicationLaunchResult(
            success=True,
            message=f"Successfully launched {display_name}.",
            application=display_name,
            resolved_target=target_executable,
        )
    except Exception as exc:
        return ApplicationLaunchResult(
            success=False,
            message=f"Failed to launch '{display_name}': {exc}",
            application=display_name,
            resolved_target=target_executable,
            error=str(exc),
        )


def get_running_process_names() -> list[tuple[int, str]]:
    """
    Enumerate running processes on the system using direct OS APIs.
    Returns list of (pid, process_exe_name_lower).
    Uses safe Windows Toolhelp32 snapshot via ctypes on Windows, avoiding shell execution.
    """
    results: list[tuple[int, str]] = []
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            kernel32 = ctypes.windll.kernel32
            hSnapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if hSnapshot and hSnapshot != -1:
                entry = PROCESSENTRY32()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
                if kernel32.Process32First(hSnapshot, ctypes.byref(entry)):
                    while True:
                        pid = int(entry.th32ProcessID)
                        name = entry.szExeFile.decode("utf-8", errors="ignore").lower()
                        results.append((pid, name))
                        if not kernel32.Process32Next(hSnapshot, ctypes.byref(entry)):
                            break
                kernel32.CloseHandle(hSnapshot)
        except Exception:
            pass
    return results


def terminate_process_by_pid(pid: int) -> bool:
    """
    Safely terminate a process by PID using standard library direct OS signals.
    Never uses shell=True, command construction, or arbitrary strings.
    """
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def check_application_running(raw_name: str) -> ApplicationStatusResult:
    """
    Check whether an allowlisted application is currently running.
    Resolves aliases, inspects registered process names, never executes arbitrary processes.
    """
    resolved = resolve_application(raw_name)
    if not resolved:
        return ApplicationStatusResult(
            success=False,
            application=raw_name,
            running=False,
            process_count=0,
            message=f"'{raw_name}' is not in the allowed application list.",
            error="UNKNOWN_APPLICATION",
        )

    canonical_name, spec = resolved
    display_name = spec.get("display_name", canonical_name)
    registered_procs = [p.lower() for p in spec.get("process_names", [])]

    if not registered_procs:
        return ApplicationStatusResult(
            success=False,
            application=display_name,
            running=False,
            process_count=0,
            message=f"No process definitions found for '{display_name}'.",
            error="NO_PROCESS_SPEC",
        )

    procs = get_running_process_names()
    matching_pids = [
        pid for pid, name in procs
        if name in registered_procs
    ]

    is_running = len(matching_pids) > 0
    if is_running:
        status_msg = f"Yes, {display_name} is currently running."
    else:
        status_msg = f"No, {display_name} is not currently running."

    return ApplicationStatusResult(
        success=True,
        application=display_name,
        running=is_running,
        process_count=len(matching_pids),
        message=status_msg,
    )


def close_application(raw_name: str) -> ApplicationCloseResult:
    """
    Safely close an allowlisted application.
    Enforces strict allowlist validation, alias resolution, protected application policy,
    and terminates only approved matching process identities.
    """
    resolved = resolve_application(raw_name)
    if not resolved:
        return ApplicationCloseResult(
            success=False,
            message=f"'{raw_name}' is not in the allowed application list.",
            application=raw_name,
            error="UNKNOWN_APPLICATION",
        )

    canonical_name, spec = resolved
    display_name = spec.get("display_name", canonical_name)

    # Check closable policy
    if not spec.get("closable", True):
        return ApplicationCloseResult(
            success=False,
            message=f"JARVIS is not allowed to close {display_name}.",
            application=display_name,
            error="APPLICATION_NOT_CLOSABLE",
        )

    registered_procs = [p.lower() for p in spec.get("process_names", [])]
    if not registered_procs:
        return ApplicationCloseResult(
            success=False,
            message=f"No process definitions found for '{display_name}'.",
            application=display_name,
            error="NO_PROCESS_SPEC",
        )

    # Check running processes
    procs = get_running_process_names()
    matching_pids = [
        pid for pid, name in procs
        if name in registered_procs
    ]

    if not matching_pids:
        return ApplicationCloseResult(
            success=True,
            message=f"{display_name} is not currently running.",
            application=display_name,
            terminated_count=0,
        )

    # Terminate matching approved PIDs
    terminated = 0
    for pid in matching_pids:
        if terminate_process_by_pid(pid):
            terminated += 1

    return ApplicationCloseResult(
        success=True,
        message=f"Successfully closed {display_name}.",
        application=display_name,
        terminated_count=terminated,
    )

