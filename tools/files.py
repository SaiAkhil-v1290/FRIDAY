import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_RESULTS = 100

SAFE_SEARCH_ROOTS: dict[str, Path] = {
    "desktop": Path.home() / "Desktop",
    "documents": Path.home() / "Documents",
    "downloads": Path.home() / "Downloads",
}

DEFAULT_EXCLUDED_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
}


def format_file_size(size_bytes: int | None) -> str:
    """Format bytes into human-readable file size string (B, KB, MB, GB)."""
    if size_bytes is None:
        return "Unknown size"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


@dataclass
class FileInfo:
    name: str
    path: str
    is_directory: bool
    extension: str | None = None
    size: int | None = None
    modified_time: float | None = None

    @property
    def human_size(self) -> str:
        return format_file_size(self.size)

    @property
    def formatted_modified_time(self) -> str | None:
        if self.modified_time is None:
            return None
        dt = datetime.datetime.fromtimestamp(self.modified_time)
        return dt.strftime("%Y-%m-%d %H:%M")


@dataclass
class FileSearchResult:
    success: bool
    message: str
    files: list[FileInfo]
    error: str | None = None
    limited: bool = False
    directories: list[FileInfo] = None  # Populated in list mode

    def __post_init__(self):
        if self.directories is None:
            self.directories = []


def normalize_root_key(location_name: str | None) -> str | None:
    """Normalize a user-provided location string to a safe root key, or None."""
    if not location_name or not isinstance(location_name, str):
        return None
    cleaned = location_name.strip().lower()
    # Strip common suffixes/prefixes
    cleaned = cleaned.replace("folder", "").replace("directory", "").strip()
    if cleaned.startswith("my "):
        cleaned = cleaned[3:].strip()
    if cleaned in SAFE_SEARCH_ROOTS:
        return cleaned
    return None


def resolve_safe_root(location: str | Path | None) -> tuple[Path | None, str | None]:
    """
    Resolve and validate a search location against SAFE_SEARCH_ROOTS.
    Returns (resolved_path, None) if safe, or (None, error_code).
    """
    if location is None:
        return None, None

    loc_str = str(location).strip()
    if not loc_str:
        return None, None

    # Check named root first (e.g. "downloads", "Downloads", "my documents folder")
    named_key = normalize_root_key(loc_str)
    if named_key:
        target_root = SAFE_SEARCH_ROOTS[named_key]
        resolved_root = target_root.resolve()
        return resolved_root, None

    # Check path-like location
    try:
        candidate_path = Path(loc_str)
        # If relative, check against safe roots
        if not candidate_path.is_absolute():
            # Check if it matches a known folder name directly
            norm_name = loc_str.lower().rstrip("/\\")
            if norm_name in SAFE_SEARCH_ROOTS:
                return SAFE_SEARCH_ROOTS[norm_name].resolve(), None
            # Otherwise resolve relative to current dir or reject
            # Safer policy: Relative paths must be strictly checked
            resolved_candidate = candidate_path.resolve()
        else:
            resolved_candidate = candidate_path.resolve()

        # Check containment inside any SAFE_SEARCH_ROOTS
        for root_path in SAFE_SEARCH_ROOTS.values():
            resolved_root = root_path.resolve()
            try:
                # relative_to raises ValueError if not contained
                resolved_candidate.relative_to(resolved_root)
                # Contained!
                return resolved_candidate, None
            except ValueError:
                continue

        # If not contained in any safe root:
        return None, "UNSAFE_LOCATION"

    except Exception:
        return None, "INVALID_PATH"


def is_path_safe_and_contained(target: Path, root: Path) -> bool:
    """Check if target resolves strictly within root (path traversal and symlink escape protection)."""
    try:
        resolved_target = target.resolve()
        resolved_root = root.resolve()
        resolved_target.relative_to(resolved_root)
        return True
    except (ValueError, RuntimeError, OSError):
        return False


def get_time_range(time_filter: str | None, now: datetime.datetime | None = None) -> tuple[float | None, float | None]:
    """
    Returns (start_timestamp, end_timestamp) for a given time filter name.
    Supported: 'today', 'yesterday', 'this_week'.
    Week boundary: Monday 00:00:00 of the current week until current time.
    """
    if not time_filter:
        return None, None

    if now is None:
        now = datetime.datetime.now()

    today_start = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)

    tf = time_filter.strip().lower()
    if tf == "today":
        return today_start.timestamp(), now.timestamp()
    elif tf == "yesterday":
        yesterday_start = today_start - datetime.timedelta(days=1)
        return yesterday_start.timestamp(), today_start.timestamp()
    elif tf in {"this_week", "this week", "week"}:
        # Current local week starting Monday 00:00:00
        week_start = today_start - datetime.timedelta(days=now.weekday())
        return week_start.timestamp(), now.timestamp()

    return None, None


def discover_files(
    query: str | None = None,
    extension: str | None = None,
    extensions: list[str] | None = None,
    location: str | None = None,
    max_results: int = MAX_RESULTS,
    recursive: bool = True,
    sort_by: str | None = None,
    sort_order: str | None = None,
    time_filter: str | None = None,
    size_filter: dict | None = None,
    now: datetime.datetime | None = None,
) -> FileSearchResult:
    """
    Safely discover and list files inside explicitly allowed safe roots.
    STRICTLY READ-ONLY:
    - Never modifies, moves, deletes, renames, writes, or executes files.
    - Never invokes a shell, cmd, powershell, or subprocess.
    - Traversal bounded strictly to SAFE_SEARCH_ROOTS.
    - Resolves symlinks to ensure they do not escape safe roots.
    - Limits results to max_results to maintain responsiveness.
    - Supports recursive search (default) and immediate directory listing (recursive=False).
    - Supports filtering by multiple extensions, time (today, yesterday, this_week), and size.
    - Supports sorting by modified_time or size (asc/desc).
    """
    # 1. Resolve search roots
    resolved_single_root: Path | None = None
    if location is not None and str(location).strip():
        root_path, err = resolve_safe_root(location)
        if err or root_path is None:
            return FileSearchResult(
                success=False,
                message=f"Location '{location}' is not an allowed safe search location. Safe locations: Desktop, Documents, Downloads.",
                files=[],
                directories=[],
                error=err or "UNSAFE_LOCATION",
            )
        target_roots = [root_path]
    else:
        # Default: Search all safe roots
        target_roots = [p.resolve() for p in SAFE_SEARCH_ROOTS.values()]

    # 2. Normalize and validate query & extensions
    norm_query = str(query).strip().lower() if query is not None else None
    if norm_query == "":
        norm_query = None

    # Natural query terms & keywords extraction
    query_keywords: list[str] = []
    if norm_query:
        from ai.intent import normalize_file_query_terms
        prim_t, kws_t, ext_t, exts_t = normalize_file_query_terms(norm_query)
        if kws_t:
            query_keywords = kws_t
        if ext_t and not extension:
            extension = ext_t
        if exts_t and not extensions:
            extensions = exts_t
        if prim_t and not ext_t and not exts_t:
            norm_query = prim_t

    # Handle multiple extensions or single extension
    target_extensions: set[str] = set()
    if extensions:
        for ext in extensions:
            e = str(ext).strip().lower()
            if e:
                target_extensions.add(e if e.startswith(".") else f".{e}")
    if extension:
        e = str(extension).strip().lower()
        if e:
            target_extensions.add(e if e.startswith(".") else f".{e}")

    # If query looks like an extension (e.g. ".py" or "*.py"), handle appropriately
    if norm_query and not target_extensions:
        if norm_query.startswith("*.") and len(norm_query) > 2:
            target_extensions.add(norm_query[1:])
            norm_query = None
        elif norm_query.startswith(".") and len(norm_query) > 1 and " " not in norm_query:
            target_extensions.add(norm_query)
            norm_query = None

    # Time boundaries
    t_start, t_end = get_time_range(time_filter, now=now)

    # Size filter criteria
    size_op = None
    size_bytes = None
    if size_filter and isinstance(size_filter, dict):
        size_op = size_filter.get("operator")  # "gt" or "lt"
        size_bytes = size_filter.get("bytes")

    results: list[FileInfo] = []
    dir_results: list[FileInfo] = []
    is_limited = False

    # -------------------------------------------------------------
    # 3A. Non-recursive Directory Listing Mode (Part 3)
    # -------------------------------------------------------------
    if not recursive:
        for root_path in target_roots:
            if not root_path.exists():
                continue
            try:
                with os.scandir(str(root_path)) as entries:
                    for entry in entries:
                        entry_path = Path(entry.path)
                        # Symlink escape protection
                        if not is_path_safe_and_contained(entry_path, root_path):
                            continue

                        # Exclude hidden or common excluded developer directories
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in DEFAULT_EXCLUDED_DIRS:
                                continue
                            dir_results.append(
                                FileInfo(
                                    name=entry.name,
                                    path=str(entry_path),
                                    is_directory=True,
                                    extension=None,
                                    size=None,
                                    modified_time=None,
                                )
                            )
                        elif entry.is_file(follow_symlinks=False):
                            file_ext = entry_path.suffix.lower() if entry_path.suffix else None
                            # If extension filter given
                            if target_extensions and file_ext not in target_extensions:
                                continue
                            # If query filter given
                            if norm_query:
                                from ai.intent import candidate_matches_query
                                if not candidate_matches_query(entry.name, norm_query, query_keywords):
                                    continue

                            # Basic metadata
                            f_size = None
                            f_mtime = None
                            try:
                                stat_info = entry.stat(follow_symlinks=False)
                                f_size = stat_info.st_size
                                f_mtime = stat_info.st_mtime
                            except (OSError, PermissionError):
                                pass

                            # Time filter check
                            if f_mtime is not None:
                                if t_start is not None and f_mtime < t_start:
                                    continue
                                if t_end is not None and f_mtime > t_end:
                                    continue
                            elif t_start is not None or t_end is not None:
                                continue

                            # Size filter check
                            if size_op and size_bytes is not None:
                                if f_size is None:
                                    continue
                                if size_op == "gt" and f_size <= size_bytes:
                                    continue
                                if size_op == "lt" and f_size >= size_bytes:
                                    continue

                            results.append(
                                FileInfo(
                                    name=entry.name,
                                    path=str(entry_path),
                                    is_directory=False,
                                    extension=file_ext,
                                    size=f_size,
                                    modified_time=f_mtime,
                                )
                            )

                        if len(results) + len(dir_results) >= max_results:
                            is_limited = True
                            break

            except (PermissionError, OSError):
                continue

            if is_limited:
                break

        # Sort alphabetically
        dir_results.sort(key=lambda d: d.name.lower())
        results.sort(key=lambda f: f.name.lower())

        loc_name = location.strip().capitalize() if location and str(location).strip() else "Directory"
        if not dir_results and not results:
            msg = f"{loc_name} contains no files or folders."
        else:
            msg = f"{loc_name} contains:"

        return FileSearchResult(
            success=True,
            message=msg,
            files=results,
            directories=dir_results,
            limited=is_limited,
        )

    # -------------------------------------------------------------
    # 3B. Recursive Search Mode (Part 1A)
    # -------------------------------------------------------------
    for root_path in target_roots:
        if not root_path.exists():
            continue

        try:
            # os.scandir / os.walk with error handling for inaccessible folders
            for current_dir, dirs, files in os.walk(str(root_path), followlinks=False):
                current_dir_path = Path(current_dir)

                # Prune excluded directories (case-insensitive) during traversal (Bug 4)
                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in DEFAULT_EXCLUDED_DIRS
                ]

                # Symlink escape protection: ensure current_dir is still inside root_path
                if not is_path_safe_and_contained(current_dir_path, root_path):
                    # Skip traversal into escaped directory
                    dirs.clear()
                    continue

                # Filter directories that might be symlinks pointing outside
                safe_dirs = []
                for d in dirs:
                    d_path = current_dir_path / d
                    try:
                        if is_path_safe_and_contained(d_path, root_path):
                            safe_dirs.append(d)
                    except (OSError, RuntimeError):
                        continue
                dirs[:] = safe_dirs

                for filename in files:
                    file_path = current_dir_path / filename

                    # Verify containment
                    try:
                        if not is_path_safe_and_contained(file_path, root_path):
                            continue
                    except (OSError, RuntimeError):
                        continue

                    # Extension matching
                    file_ext = file_path.suffix.lower() if file_path.suffix else None
                    if target_extensions and file_ext not in target_extensions:
                        continue

                    # Query / partial filename matching
                    if norm_query:
                        from ai.intent import candidate_matches_query
                        if not candidate_matches_query(filename, norm_query, query_keywords):
                            continue

                    f_size = None
                    f_mtime = None
                    try:
                        stat_res = file_path.stat()
                        f_size = stat_res.st_size
                        f_mtime = stat_res.st_mtime
                    except (OSError, PermissionError):
                        pass

                    # Time filter check
                    if f_mtime is not None:
                        if t_start is not None and f_mtime < t_start:
                            continue
                        if t_end is not None and f_mtime > t_end:
                            continue
                    elif t_start is not None or t_end is not None:
                        continue

                    # Size filter check
                    if size_op and size_bytes is not None:
                        if f_size is None:
                            continue
                        if size_op == "gt" and f_size <= size_bytes:
                            continue
                        if size_op == "lt" and f_size >= size_bytes:
                            continue

                    results.append(
                        FileInfo(
                            name=filename,
                            path=str(file_path),
                            is_directory=False,
                            extension=file_ext,
                            size=f_size,
                            modified_time=f_mtime,
                        )
                    )

                    if len(results) >= max_results:
                        is_limited = True
                        break

                if is_limited:
                    break

        except (PermissionError, OSError) as exc:
            # Inaccessible directory - safely continue without crashing
            continue

        if is_limited:
            break

    # 4. Result sorting / ranking:
    # If explicit sorting requested, apply it
    norm_sort = sort_by.strip().lower() if sort_by else None
    norm_order = sort_order.strip().lower() if sort_order else "desc"
    reverse = (norm_order != "asc")

    if norm_sort in {"modified_time", "mtime", "time", "date"}:
        results.sort(key=lambda f: f.modified_time or 0.0, reverse=reverse)
    elif norm_sort in {"size", "file_size"}:
        results.sort(key=lambda f: f.size or 0, reverse=reverse)
    else:
        # Default ranking:
        # 1. Exact filename matches
        # 2. Exact case-insensitive filename matches
        # 3. Partial filename matches
        if norm_query and results:
            exact_matches: list[FileInfo] = []
            partial_matches: list[FileInfo] = []
            for fi in results:
                if fi.name.lower() == norm_query:
                    exact_matches.append(fi)
                else:
                    partial_matches.append(fi)
            results = exact_matches + partial_matches

    # Format user-friendly message
    count = len(results)
    loc_display = f" in {location.strip().capitalize()}" if (location and str(location).strip()) else ""
    sort_display = ""
    if norm_sort in {"modified_time", "mtime", "time", "date"}:
        sort_display = f", sorted by {'newest' if reverse else 'oldest'}"
    elif norm_sort in {"size", "file_size"}:
        sort_display = f", sorted by {'largest' if reverse else 'smallest'}"

    if count == 0:
        msg = f"No matching files were found in the allowed search locations{loc_display}."
    elif is_limited:
        msg = f"Found {count} files{loc_display}{sort_display} (showing first {max_results} results):"
    elif not norm_query and not target_extensions and loc_display:
        msg = f"Found {count} file{'s' if count != 1 else ''}{loc_display}{sort_display}:"
    else:
        msg = f"Found {count} matching file{'s' if count != 1 else ''}{loc_display}{sort_display}:"

    return FileSearchResult(
        success=True,
        message=msg,
        files=results,
        directories=[],
        limited=is_limited,
    )


MAX_CONTENT_FILE_SIZE = 5 * 1024 * 1024  # 5 MB max per file for content scanning
MAX_SNIPPET_LENGTH = 150  # Max characters for a line snippet
MAX_CONTENT_MATCHES = 50

# Supported text-based extensions for safe content searching
TEXT_CONTENT_EXTENSIONS: set[str] = {
    ".txt",
    ".py",
    ".md",
    ".json",
    ".csv",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".cfg",
    ".log",
    ".rtf",
}

DOCUMENT_CONTENT_EXTENSIONS: set[str] = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
}

SUPPORTED_CONTENT_EXTENSIONS: set[str] = (
    TEXT_CONTENT_EXTENSIONS | DOCUMENT_CONTENT_EXTENSIONS
)


@dataclass
class ContentMatch:
    name: str
    path: str
    location: str  # e.g. "Line 42", "Page 7", "Paragraph 18", "Slide 4", 'Sheet "Results", Cell B14'
    snippet: str
    line_number: int | None = None  # Backward compatibility for text files


@dataclass
class ContentSearchResult:
    success: bool
    message: str
    matches: list[ContentMatch]
    error: str | None = None
    limited: bool = False


@dataclass
class SearchableUnit:
    location: str
    text: str
    line_number: int | None = None


def extract_text_file_units(file_path: Path):
    """Line-by-line generator of SearchableUnits for plain text files."""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line_idx, line in enumerate(f, start=1):
            yield SearchableUnit(location=f"Line {line_idx}", text=line, line_number=line_idx)


def extract_pdf_units(file_path: Path):
    """Page-by-page generator of SearchableUnits for PDF documents using pypdf."""
    import pypdf
    with open(file_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page_idx, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text()
            except Exception:
                continue
            if page_text:
                for line in page_text.splitlines():
                    clean = line.strip()
                    if clean:
                        yield SearchableUnit(location=f"Page {page_idx}", text=clean)


def extract_docx_units(file_path: Path):
    """Paragraph-by-paragraph generator of SearchableUnits for Word documents."""
    import docx
    doc = docx.Document(str(file_path))
    for p_idx, paragraph in enumerate(doc.paragraphs, start=1):
        clean = paragraph.text.strip()
        if clean:
            yield SearchableUnit(location=f"Paragraph {p_idx}", text=clean)
    # Also search tables in DOCX
    for t_idx, table in enumerate(doc.tables, start=1):
        for r_idx, row in enumerate(table.rows, start=1):
            for c_idx, cell in enumerate(row.cells, start=1):
                clean = cell.text.strip()
                if clean:
                    yield SearchableUnit(location=f"Table {t_idx}, Row {r_idx}", text=clean)


def extract_pptx_units(file_path: Path):
    """Slide-by-slide generator of SearchableUnits for PowerPoint presentations."""
    import pptx
    prs = pptx.Presentation(str(file_path))
    for s_idx, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    clean = paragraph.text.strip()
                    if clean:
                        yield SearchableUnit(location=f"Slide {s_idx}", text=clean)


def extract_xlsx_units(file_path: Path):
    """Cell-by-cell generator of SearchableUnits for Excel workbooks."""
    import openpyxl
    # read_only=True and data_only=True ensure fast, memory-safe, non-executing read
    wb = openpyxl.load_workbook(str(file_path), read_only=True, data_only=True)
    try:
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            for row in sheet.iter_rows(values_only=False):
                for cell in row:
                    if cell.value is not None:
                        val_str = str(cell.value).strip()
                        if val_str:
                            cell_loc = cell.coordinate if hasattr(cell, "coordinate") and cell.coordinate else f"Row {cell.row}, Col {cell.column}"
                            yield SearchableUnit(location=f'Sheet "{sheet_name}", Cell {cell_loc}', text=val_str)
    finally:
        wb.close()


def extract_searchable_units(file_path: Path, file_ext: str):
    """Dispatch file extraction based on extension."""
    if file_ext in TEXT_CONTENT_EXTENSIONS:
        return extract_text_file_units(file_path)
    elif file_ext == ".pdf":
        return extract_pdf_units(file_path)
    elif file_ext == ".docx":
        return extract_docx_units(file_path)
    elif file_ext == ".pptx":
        return extract_pptx_units(file_path)
    elif file_ext == ".xlsx":
        return extract_xlsx_units(file_path)
    return ()


def search_file_contents(
    query: str,
    location: str | None = None,
    extensions: list[str] | set[str] | None = None,
    max_results: int = MAX_CONTENT_MATCHES,
    max_file_size: int = MAX_CONTENT_FILE_SIZE,
) -> ContentSearchResult:
    """
    Safely search inside supported text files for literal text query.
    STRICTLY READ-ONLY:
    - Never modifies, moves, deletes, renames, writes, or executes files.
    - Never invokes a shell, cmd, powershell, or subprocess.
    - Traversal bounded strictly to SAFE_SEARCH_ROOTS.
    - Resolves symlinks to ensure they do not escape safe roots.
    - Only reads supported text extensions (SUPPORTED_CONTENT_EXTENSIONS).
    - Ignores binary files (.exe, .dll, .png, .jpg, .zip, etc.).
    - Skips files exceeding max_file_size to prevent memory issues.
    - Line-by-line streaming without loading entire files into memory.
    - Case-insensitive literal matching (no arbitrary regex eval).
    - Returns bounded snippets with line numbers.
    """
    if not query or not str(query).strip():
        return ContentSearchResult(
            success=False,
            message="No search query provided for content search.",
            matches=[],
            error="EMPTY_QUERY",
        )

    clean_query = str(query).strip()
    query_lower = clean_query.lower()

    # 1. Resolve search roots
    if location is not None and str(location).strip():
        root_path, err = resolve_safe_root(location)
        if err or root_path is None:
            return ContentSearchResult(
                success=False,
                message=f"Location '{location}' is not an allowed safe search location. Safe locations: Desktop, Documents, Downloads.",
                matches=[],
                error=err or "UNSAFE_LOCATION",
            )
        target_roots = [root_path]
    else:
        # Default: Search all safe roots
        target_roots = [p.resolve() for p in SAFE_SEARCH_ROOTS.values()]

    # 2. Resolve allowed extensions
    allowed_exts: set[str] = set()
    if extensions:
        for ext in extensions:
            e = str(ext).strip().lower()
            if not e.startswith("."):
                e = f".{e}"
            if e in SUPPORTED_CONTENT_EXTENSIONS:
                allowed_exts.add(e)
    else:
        allowed_exts = set(SUPPORTED_CONTENT_EXTENSIONS)

    matches: list[ContentMatch] = []
    is_limited = False

    for root_path in target_roots:
        if not root_path.exists():
            continue

        try:
            for current_dir, dirs, files in os.walk(str(root_path), followlinks=False):
                current_dir_path = Path(current_dir)

                # Prune excluded directories (case-insensitive)
                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in DEFAULT_EXCLUDED_DIRS
                ]

                # Symlink escape protection
                if not is_path_safe_and_contained(current_dir_path, root_path):
                    dirs.clear()
                    continue

                safe_dirs = []
                for d in dirs:
                    d_path = current_dir_path / d
                    try:
                        if is_path_safe_and_contained(d_path, root_path):
                            safe_dirs.append(d)
                    except (OSError, RuntimeError):
                        continue
                dirs[:] = safe_dirs

                for filename in files:
                    file_path = current_dir_path / filename

                    # Verify containment
                    try:
                        if not is_path_safe_and_contained(file_path, root_path):
                            continue
                    except (OSError, RuntimeError):
                        continue

                    # Extension check: must be in allowed text extensions
                    file_ext = file_path.suffix.lower() if file_path.suffix else ""
                    if file_ext not in allowed_exts:
                        continue

                    # File size check: skip oversized files to protect memory
                    try:
                        f_size = file_path.stat().st_size
                        if f_size > max_file_size or f_size == 0:
                            continue
                    except (OSError, PermissionError):
                        continue

                    # Safe unit streaming inspection (text, PDF, DOCX, PPTX, XLSX)
                    try:
                        units = extract_searchable_units(file_path, file_ext)
                        for unit in units:
                            if query_lower in unit.text.lower():
                                clean_line = unit.text.strip()
                                if len(clean_line) > MAX_SNIPPET_LENGTH:
                                    match_pos = clean_line.lower().find(query_lower)
                                    start_pos = max(0, match_pos - 40)
                                    end_pos = min(len(clean_line), match_pos + len(query_lower) + 40)
                                    snippet = ("..." if start_pos > 0 else "") + clean_line[start_pos:end_pos] + ("..." if end_pos < len(clean_line) else "")
                                else:
                                    snippet = clean_line

                                matches.append(
                                    ContentMatch(
                                        name=filename,
                                        path=str(file_path),
                                        location=unit.location,
                                        snippet=snippet,
                                        line_number=unit.line_number,
                                    )
                                )
                                # Record first match per file
                                if len(matches) >= max_results:
                                    is_limited = True
                                    break
                                break

                    except Exception:
                        # Malformed or unreadable documents do not crash the search
                        continue

                    if is_limited:
                        break

                if is_limited:
                    break

        except (PermissionError, OSError):
            continue

        if is_limited:
            break

    count = len(matches)
    loc_display = f" in {location.strip().capitalize()}" if (location and str(location).strip()) else ""
    if count == 0:
        msg = f"No supported files containing \"{clean_query}\" were found in the allowed search locations{loc_display}."
    elif is_limited:
        msg = f"Found {count} files containing \"{clean_query}\"{loc_display} (showing first {max_results} results):"
    else:
        msg = f"Found {count} file{'s' if count != 1 else ''} containing \"{clean_query}\"{loc_display}:"

    return ContentSearchResult(
        success=True,
        message=msg,
        matches=matches,
        limited=is_limited,
    )


@dataclass
class FileOpenResult:
    success: bool
    message: str
    path: str | None = None
    error: str | None = None


def open_local_file(file_path_str: str) -> FileOpenResult:
    """
    Safely open a local file inside allowed safe roots.
    Guarantees:
    - Path must resolve strictly inside SAFE_SEARCH_ROOTS (Desktop, Documents, Downloads).
    - Traversal outside safe roots (e.g. C:\\Windows) is rejected with UNSAFE_LOCATION.
    - Path must exist and must be a file, not a directory.
    - Dispatches safely via os.startfile on Windows (or xdg-open on non-Windows) with no shell=True.
    - Catches OS errors gracefully and returns clean diagnostic messages.
    """
    if not file_path_str or not str(file_path_str).strip():
        return FileOpenResult(
            success=False,
            message="No file path provided to open.",
            error="EMPTY_PATH",
        )

    clean_path = str(file_path_str).strip().strip("'\"")

    try:
        candidate = Path(clean_path)
        resolved_candidate = candidate.resolve()
    except Exception:
        return FileOpenResult(
            success=False,
            message=f"Invalid file path: '{clean_path}'.",
            error="INVALID_PATH",
        )

    # Validate safe root containment
    is_safe = False
    for root in SAFE_SEARCH_ROOTS.values():
        try:
            resolved_root = root.resolve()
            resolved_candidate.relative_to(resolved_root)
            is_safe = True
            break
        except (ValueError, RuntimeError):
            continue

    if not is_safe:
        return FileOpenResult(
            success=False,
            message="The file could not be opened safely because it is outside the allowed locations. Allowed locations: Desktop, Documents, Downloads.",
            path=str(resolved_candidate),
            error="UNSAFE_LOCATION",
        )

    try:
        if not resolved_candidate.exists():
            return FileOpenResult(
                success=False,
                message=f"File not found: '{resolved_candidate.name}'.",
                path=str(resolved_candidate),
                error="FILE_NOT_FOUND",
            )

        if not resolved_candidate.is_file():
            return FileOpenResult(
                success=False,
                message=f"'{resolved_candidate.name}' is a directory, not a file.",
                path=str(resolved_candidate),
                error="NOT_A_FILE",
            )
    except PermissionError:
        return FileOpenResult(
            success=False,
            message=f"Access denied to file: '{resolved_candidate.name}'.",
            path=str(resolved_candidate),
            error="ACCESS_DENIED",
        )

    # Launch file via OS default viewer without shell=True
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(resolved_candidate))
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(resolved_candidate)], shell=False)

        return FileOpenResult(
            success=True,
            message=f"Opened: {resolved_candidate.name}",
            path=str(resolved_candidate),
        )
    except Exception as exc:
        return FileOpenResult(
            success=False,
            message=f"Failed to open '{resolved_candidate.name}': {str(exc)}",
            path=str(resolved_candidate),
            error="OPEN_FAILED",
        )


@dataclass
class FileRevealResult:
    success: bool
    message: str
    path: str
    error: str | None = None


def reveal_local_file(file_path: str) -> FileRevealResult:
    """
    Safely reveal a file in File Explorer or open its parent folder.
    Validates that:
    1. The path is non-empty.
    2. The path is within SAFE_SEARCH_ROOTS (Desktop, Documents, Downloads).
    3. The file exists and is a file.
    4. Safe execution without shell=True.
    """
    if not file_path or not file_path.strip():
        return FileRevealResult(
            success=False,
            message="No file path provided to reveal.",
            path="",
            error="EMPTY_PATH",
        )

    candidate_path = Path(file_path)
    try:
        resolved_candidate = candidate_path.resolve()
    except (ValueError, RuntimeError, OSError) as e:
        return FileRevealResult(
            success=False,
            message=f"Invalid path traversal: {e}",
            path=file_path,
            error="INVALID_PATH",
        )

    # Validate safe root containment
    is_safe = False
    for root in SAFE_SEARCH_ROOTS.values():
        try:
            resolved_root = root.resolve()
            resolved_candidate.relative_to(resolved_root)
            is_safe = True
            break
        except (ValueError, RuntimeError):
            continue

    if not is_safe:
        return FileRevealResult(
            success=False,
            message="The file location could not be opened safely because it is outside the allowed locations. Allowed locations: Desktop, Documents, Downloads.",
            path=str(resolved_candidate),
            error="UNSAFE_LOCATION",
        )

    try:
        if not resolved_candidate.exists():
            return FileRevealResult(
                success=False,
                message=f"File not found: '{resolved_candidate.name}'.",
                path=str(resolved_candidate),
                error="FILE_NOT_FOUND",
            )

        if not resolved_candidate.is_file():
            return FileRevealResult(
                success=False,
                message=f"'{resolved_candidate.name}' is a directory, not a file.",
                path=str(resolved_candidate),
                error="NOT_A_FILE",
            )
    except PermissionError:
        return FileRevealResult(
            success=False,
            message=f"Access denied to file: '{resolved_candidate.name}'.",
            path=str(resolved_candidate),
            error="ACCESS_DENIED",
        )

    # Launch file explorer revealing/selecting the file without shell=True
    try:
        parent_dir = resolved_candidate.parent
        import sys
        import subprocess
        if sys.platform == "win32":
            # On Windows, explorer.exe /select,"<path>" safely selects the file in Explorer
            subprocess.Popen(["explorer.exe", f"/select,{str(resolved_candidate)}"], shell=False)
        elif hasattr(os, "startfile"):
            os.startfile(str(parent_dir))
        else:
            subprocess.Popen(["xdg-open", str(parent_dir)], shell=False)

        return FileRevealResult(
            success=True,
            message=f"Revealed location: {resolved_candidate.name}",
            path=str(resolved_candidate),
        )
    except Exception as exc:
        return FileRevealResult(
            success=False,
            message=f"Failed to open folder containing '{resolved_candidate.name}': {str(exc)}",
            path=str(resolved_candidate),
            error="REVEAL_FAILED",
        )


@dataclass
class FolderOpenResult:
    success: bool
    message: str
    folder: str
    path: str | None = None
    error: str | None = None


def open_local_folder(folder_target: str) -> FolderOpenResult:
    """
    Safely open a local user folder (Desktop, Documents, Downloads).
    Milestone 4D.1 - Natural Language Folder Control.
    Guarantees:
    - Never uses shell=True.
    - Only allows folders inside SAFE_SEARCH_ROOTS (Desktop, Documents, Downloads).
    - Arbitrary system paths (e.g. C:\\Windows\\System32) are strictly rejected as UNSAFE_LOCATION.
    - Non-existent directories are rejected with FOLDER_NOT_FOUND.
    """
    if not folder_target or not str(folder_target).strip():
        return FolderOpenResult(
            success=False,
            message="No folder specified.",
            folder=str(folder_target or ""),
            error="MISSING_FOLDER",
        )

    raw_target = str(folder_target).strip().strip("'\"")
    cleaned_lower = raw_target.lower()

    # Strip conversational prefixes/suffixes like "my downloads folder" -> "downloads"
    import re
    m_clean = re.match(
        r"^(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$",
        cleaned_lower,
    )
    if m_clean:
        canonical_key = m_clean.group(1)
        target_dir = SAFE_SEARCH_ROOTS[canonical_key]
    else:
        # Check if direct key in SAFE_SEARCH_ROOTS
        if cleaned_lower in SAFE_SEARCH_ROOTS:
            target_dir = SAFE_SEARCH_ROOTS[cleaned_lower]
        else:
            target_dir = Path(raw_target)

    # Validate against SAFE_SEARCH_ROOTS
    try:
        resolved_candidate = target_dir.resolve()
    except Exception as exc:
        return FolderOpenResult(
            success=False,
            message=f"Invalid folder path: '{raw_target}'.",
            folder=raw_target,
            error="INVALID_PATH",
        )

    is_safe = False
    for safe_root in SAFE_SEARCH_ROOTS.values():
        try:
            resolved_root = safe_root.resolve()
            # The candidate must either be equal to the safe root or a subfolder inside it
            if resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents:
                is_safe = True
                break
        except Exception:
            continue

    if not is_safe:
        return FolderOpenResult(
            success=False,
            message="The folder could not be opened safely because it is outside the allowed locations. Allowed locations: Desktop, Documents, Downloads.",
            folder=raw_target,
            path=str(resolved_candidate),
            error="UNSAFE_LOCATION",
        )

    try:
        if not resolved_candidate.exists():
            return FolderOpenResult(
                success=False,
                message=f"Folder not found: '{resolved_candidate.name}'.",
                folder=raw_target,
                path=str(resolved_candidate),
                error="FOLDER_NOT_FOUND",
            )

        if not resolved_candidate.is_dir():
            return FolderOpenResult(
                success=False,
                message=f"'{resolved_candidate.name}' is a file, not a folder.",
                folder=raw_target,
                path=str(resolved_candidate),
                error="NOT_A_FOLDER",
            )
    except PermissionError:
        return FolderOpenResult(
            success=False,
            message=f"Access denied to folder: '{resolved_candidate.name}'.",
            folder=raw_target,
            path=str(resolved_candidate),
            error="ACCESS_DENIED",
        )

    # Launch file explorer at folder without shell=True
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(resolved_candidate))
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(resolved_candidate)], shell=False)

        folder_display_name = resolved_candidate.name
        return FolderOpenResult(
            success=True,
            message=f"Opening {folder_display_name} folder: {resolved_candidate}",
            folder=raw_target,
            path=str(resolved_candidate),
        )
    except Exception as exc:
        return FolderOpenResult(
            success=False,
            message=f"Failed to open folder '{resolved_candidate.name}': {str(exc)}",
            folder=raw_target,
            path=str(resolved_candidate),
            error="OPEN_FAILED",
        )




