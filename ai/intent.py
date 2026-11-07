import re
from typing import Literal

IntentCategory = Literal[
    "continuation",
    "prompt_injection",
    "missing_input",
    "memory_store",
    "memory_forget",
    "memory_recall",
    "conversation",
    "dangerous_task",
    "unclear",
    "task",
]

# Normalized exact greetings and casual expressions
EXACT_CONVERSATIONS = {
    "hi",
    "hello",
    "hey",
    "yo",
    "hello there",
    "how are you",
    "how are you doing",
    "hows it going",
    "how are things",
    "what can you do",
    "tell me about yourself",
    "who are you",
    "thanks",
    "thank you",
    "thanks a lot",
    "good morning",
    "good night",
    "good evening",
    "good afternoon",
    "bye",
    "goodbye",
    "see you",
    "see ya",
}

# Conversational expressions with "open" that are NOT application launches
CONVERSATIONAL_OPEN_PATTERNS = [
    r"\bopen\s+to\s+suggestions\b",
    r"\bopen\s+minded\b",
    r"\bopen\s+source\b",
    r"\bopen\s+question\b",
    r"\bopen\s+discussion\b",
]

# Continuation patterns
CONTINUATION_PATTERNS = [
    r"^continue(\s+the\s+previous\s+task|\s+what\s+you\s+were\s+doing|\s+with\s+that)?$",
    r"^go\s+on(\s+with\s+that)?$",
    r"^resume(\s+the\s+previous\s+task)?$",
    r"^keep\s+going$",
]

# Explicit memory store patterns
MEMORY_STORE_PATTERNS = [
    r"^remember\s+that\b",
    r"^remember\s+this\b",
    r"^remember\b",
    r"^save\s+this\b",
    r"^save\s+that\b",
    r"^keep\s+in\s+mind\s+that\b",
    r"^keep\s+in\s+mind\b",
    r"^store\s+this\b",
    r"^add\s+this\s+to\s+your\s+memory\b",
]

# Explicit memory forget patterns
MEMORY_FORGET_PATTERNS = [
    r"^forget\s+that\b",
    r"^forget\s+my\s+preference\b",
    r"^forget\s+about\b",
    r"^forget\s+everything$",
    r"^forget\b",
    r"^remove\s+(that|this)\s+from\s+your\s+memory",
    r"^delete\s+(that|this)\s+memory",
    r"^delete\s+all\s+memories",
    r"^clear\s+my\s+memory",
    r"^wipe\s+all\s+your\s+memory",
]

# Explicit long-term memory recall patterns
LONG_TERM_RECALL_PATTERNS = [
    r"^what\s+do\s+you\s+remember\s+about\s+me",
    r"^what\s+do\s+you\s+remember\s+about\b",
    r"^what\s+do\s+you\s+remember\b",
    r"^what\s+preferences\s+do\s+you\s+remember\b",
    r"^show\s+me\s+what\s+you\s+remember\b",
    r"^list\s+(all\s+)?(my\s+)?memories\b",
]

# Historical conversation / working-memory recall patterns
WORKING_MEMORY_RECALL_PATTERNS = [
    r"\bwhat\s+did\s+i\s+(ask|say|tell|request)\b",
    r"\bwhat\s+did\s+we\s+(talk|discuss|say)\b",
    r"\bwhat\s+was\s+(the\s+previous|my\s+last|the\s+last)\s+(task|request|command|search|query|question)\b",
    r"\bremind\s+me\s+what\s+(i|we)\b",
    r"\bwhat\s+did\s+you\s+(tell|say|answer)\b",
    r"\bwhat\s+(was|were)\s+(we|i)\s+(talking|discussing|doing)\b",
    r"\bwhat\s+did\s+i\s+ask\s+you\s+to\b",
]

# Dangerous task triggers
DANGEROUS_PATTERNS = [
    r"\b(delete|remove\s+all|erase|wipe|format|destroy)\b",
    r"\b(send\s+(an?\s+)?email|send\s+(an?\s+)?message|send\s+mail)\b",
    r"\b(submit|purchase|buy|order|pay|transfer\s+money)\b",
]

# Ambiguous file deletion / cleaning requests that lack target or location
AMBIGUOUS_DELETION_PATTERNS = [
    r"^start\s+(deleting|removing|cleaning|erasing|clearing|wiping|organizing)\s+(old\s+|unnecessary\s+|temp\s+|temporary\s+)?files",
    r"^(delete|remove|clean|erase|clear|organize)\s+(old|unnecessary|some|temp|temporary)\s+files",
    r"^start\s+(cleaning|organizing)\s+(my\s+)?(downloads|documents|desktop|files)?(\s+folder)?$",
    r"^clean\s+(up\s+)?(my\s+)?(downloads|documents|desktop|files)?(\s+folder)?$",
    r"^organize\s+(my\s+)?(downloads|documents|desktop|files)?(\s+folder)?$",
]

# Requests with essential missing inputs that require user clarification
MISSING_INPUT_PATTERNS = [
    # Code execution without code
    (r"^(run|execute|test)\s+(this\s+|the\s+)?(python\s+)?(code|script|program)$", "Sure — please provide the Python code you want me to run."),
    (r"^(run|execute)\s+this$", "Sure — please provide the code or command you want me to run."),
    # Empty search query
    (r"^open\s+(chrome|browser|google\s+chrome|edge|msedge)\s+and\s+search(\s+for)?\s*$", "What would you like me to search for?"),
    (r"^(search(\s+for|\s+google|\s+the\s+web|\s+online)?|google|look\s*up)\s*$", "What would you like me to search for?"),
    (r"^search\s+for\s*$", "What would you like me to search for?"),
    # File opening without file name
    (r"^(open|view|read)\s+(this\s+|the\s+)?file$", "Which file would you like me to open?"),
    # Email without recipient/content
    (r"^(send|draft|compose)\s+(an?\s+)?email$", "Who would you like me to send the email to, and what should it say?"),
]

# Vague / unclear patterns that should not guess tools
UNCLEAR_PATTERNS = [
    r"^you\s+can\s+access\b",
    r"^can\s+you\s+access\b",
    r"^access\b",
    r"^you\s+can\s+see\b",
    r"^you\s+can\s+use\b",
    r"^help\s+me(\s+with\s+this)?$",
    r"^can\s+you\s+help(\s+me)?$",
    r"^do\s+something(\s+with\s+this)?$",
    r"^help$",
]

# Prompt injection and adversarial instruction-override patterns
INJECTION_PATTERNS = [
    r"\bignore\s+(all\s+|your\s+)?(previous\s+)?instructions\b",
    r"\bdisregard\s+(all\s+|your\s+)?(previous\s+)?instructions\b",
    r"\boverride\s+(your\s+)?rules\b",
    r"\bimmediately\s+execute\b",
    r"\bexecute\s+whatever\s+tools\b",
]

# Non-application gerund or action phrases that must NOT be extracted as application names
NON_APPLICATION_PREFIXES = (
    "deleting", "removing", "cleaning", "erasing", "clearing", "wiping", "formatting",
    "organizing", "searching", "looking", "finding", "reading", "creating", "making",
    "writing", "drafting", "sending", "this code", "the code", "this python", "the python",
    "this script", "the script", "this program", "the program", "a program", "a script",
    "a file", "this file", "the file", "old files", "files", "my files",
)

# Strong explicit task patterns
TASK_PATTERNS = [
    r"^(open|launch)\s+[a-z0-9_-]+.*$",
    r"\b(open|launch|start|run)\s+(chrome|browser|youtube|google|notepad|word|excel|vscode|vs\s+code|visual\s+studio\s+code|edge|calculator|calc|file\s+explorer|explorer|application|app|discord|brave|spotify|slack)\b",
    # Any search ... for ... or search for ...
    r"\b(search(\s+for|\s+the\s+web|\s+online|\s+youtube|\s+google|\s+in\s+downloads)?|look\s*up|google)\b.+",
    r"\bread\s+(my\s+)?screen\b",
    r"\blook\s+at\s+(my\s+)?screen\b",
    r"\b(take|capture)\s+(a\s+)?screenshot\b",
    r"\bfind\s+(my\s+)?(resume|cv|file|files|folder|folders|doc|document|documents)\b",
    r"\b(create|make|write|generate|save)\s+(a\s+)?(document|file|note|text\s+file)\b",
    r"\b(draft|compose|write)\s+(an?\s+)?(email|mail|message)\b",
    r"\b(browser_search|open_application|read_screen|find_files|create_document|draft_email|send_email|delete_files)\b",
]

# Conversational starters that indicate questions/conversation without tasks
CONVERSATIONAL_STARTERS = (
    "what can you do",
    "tell me about yourself",
    "who are you",
    "what do you think",
    "can you explain",
    "explain what",
    "explain how",
    "explain why",
    "explain this concept",
    "explain ",
    "tell me what",
    "tell me how",
    "tell me why",
    "tell me a joke",
    "tell me a story",
    "what is",
    "whats",
    "who is",
    "how does",
    "why is",
)


def is_memory_recall_request(normalized_text: str) -> bool:
    """Check if the text is asking to recall past conversation or working memory."""
    return any(re.search(pat, normalized_text) for pat in WORKING_MEMORY_RECALL_PATTERNS)


def check_missing_input(normalized_text: str) -> str | None:
    """
    Check if the user request has essential missing input (e.g. 'run this Python code').
    Returns the clarification prompt message if missing, or None.
    """
    for pat, msg in MISSING_INPUT_PATTERNS:
        if re.search(pat, normalized_text):
            return msg
    return None


COMMON_FILE_EXTENSIONS: dict[str, str | list[str]] = {
    "python": ".py",
    "py": ".py",
    "pdf": ".pdf",
    "pdfs": ".pdf",
    "text": ".txt",
    "txt": ".txt",
    "word": ".docx",
    "doc": ".doc",
    "docx": ".docx",
    "excel": ".xlsx",
    "csv": ".csv",
    "json": ".json",
    "markdown": ".md",
    "md": ".md",
    # Categories
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"],
    "images": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"],
    "png": ".png",
    "jpg": ".jpg",
    "jpeg": ".jpeg",
    "video": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "videos": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "document": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt"],
    "documents": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt"],
}

FILE_FILLER_WORDS: set[str] = {
    "my", "the", "file", "files", "document", "documents", "a", "an", "please", "all"
}


def normalize_file_query_terms(raw_query: str) -> tuple[str | None, list[str], str | None, list[str] | None]:
    """
    Normalize natural file search phrases (e.g. 'my resume', 'the resume in Downloads', 'my PDF').
    Returns (primary_search_term, keywords, single_ext, multiple_exts).
    Treats separators (_, -, spaces, parentheses, dots) consistently and removes conversational filler words.
    """
    if not raw_query or not raw_query.strip():
        return None, [], None, None

    base = raw_query.strip()
    ext = None
    ext_match = re.search(r"\.([a-zA-Z0-9]+)$", base)
    if ext_match:
        ext = "." + ext_match.group(1).lower()
        base = base[:ext_match.start()].strip()

    tokens = re.sub(r"[\s_()\-]+", " ", base).lower().split()
    keywords = [t for t in tokens if t not in FILE_FILLER_WORDS]

    inferred_exts = None
    filtered_kws = []
    for kw in keywords:
        if kw in COMMON_FILE_EXTENSIONS:
            val = COMMON_FILE_EXTENSIONS[kw]
            if isinstance(val, list):
                inferred_exts = val
            else:
                ext = val
        else:
            filtered_kws.append(kw)

    # If the user specifically said "document" or "documents", infer document extensions if not already set
    if not ext and not inferred_exts and any(t in {"document", "documents"} for t in tokens):
        inferred_exts = COMMON_FILE_EXTENSIONS.get("document")

    primary = filtered_kws[0] if len(filtered_kws) == 1 else (" ".join(filtered_kws) if filtered_kws else None)
    return primary, filtered_kws, ext, inferred_exts


def candidate_matches_query(filename: str, raw_target: str, keywords: list[str]) -> bool:
    """
    Check if a filename matches a requested file target or keywords.
    Case-insensitive, separator-aware, and avoids overly fuzzy matches.
    """
    c_lower = filename.lower()
    r_lower = raw_target.lower()
    if c_lower == r_lower or c_lower == r_lower.strip():
        return True
    c_stem = re.sub(r"\.[a-zA-Z0-9]+$", "", c_lower)
    if c_stem == r_lower:
        return True
    c_norm = re.sub(r"[\s_()\-]+", " ", c_lower)
    r_norm = re.sub(r"[\s_()\-]+", " ", r_lower)
    if r_norm in c_norm:
        return True
    if keywords:
        c_tokens = re.sub(r"[\s_()\-.]+", " ", c_lower).split()
        if all(kw in c_lower or any(kw in tok for tok in c_tokens) for kw in keywords):
            return True
    return False


def parse_size_to_bytes(number_str: str, unit_str: str) -> int | None:
    """Safely converts numeric string and unit (B, KB, MB, GB) to byte count."""
    try:
        val = float(number_str.strip())
        u = unit_str.strip().upper()
        if u in {"B", "BYTES", "BYTE"}:
            return int(val)
        elif u in {"KB", "K", "KILOBYTES", "KILOBYTE"}:
            return int(val * 1024)
        elif u in {"MB", "M", "MEGABYTES", "MEGABYTE"}:
            return int(val * 1024 * 1024)
        elif u in {"GB", "G", "GIGABYTES", "GIGABYTE"}:
            return int(val * 1024 * 1024 * 1024)
    except (ValueError, TypeError):
        return None
    return None


def extract_explicit_file_search_params(text: str) -> dict[str, str | None] | None:
    """
    Deterministically extract file discovery parameters from user input.
    Returns a dict with {"query": ..., "extension": ..., "location": ...} or None.
    Preserves security: rejects deletions, code executions, prompt injections, and missing inputs.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Injections or deletions must NOT be handled as file discovery
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat, _ in MISSING_INPUT_PATTERNS:
        if re.search(pat, norm):
            return None

    # Exclude open application / open file commands (e.g. "open this file", "open Chrome")
    if norm.startswith(("open ", "launch ", "start ", "run ")):
        return None

    # Supported search action verbs: find, show me, list, look for, search for files
    # Match patterns like:
    # "find files in downloads"
    # "find my python files"
    # "find pdf files in downloads"
    # "show me files in documents"
    # "find report.pdf"
    # "find files containing JARVIS"
    # "show me files in my Downloads folder"
    # "list my desktop files"
    # "what files are on my Desktop"

    # -------------------------------------------------------------
    # Milestone 4B.4: Explicit File Content Search Patterns
    # -------------------------------------------------------------
    # Examples:
    # "Which file contains information about botnet detection?"
    # "Find documents mentioning SentinelAI"
    # "Search inside my text files for Python"
    # "Find files containing Docker Compose"
    # "Find files that contain Docker Compose"
    # "Which files contain botnet detection?"
    # "Search file contents for Python"
    # "Find the document that mentions SentinelAI"

    content_patterns = [
        # "search inside [my] [type] files [in <location>] for <query>"
        (
            r"^(?:please\s+)?search\s+inside\s+(?:my\s+)?(text\s+files?|pdf\s+files?|pdfs?|word\s+documents?|docx\s+files?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?|documents?|notes?|files?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?\s+for\s+(.+)$",
            "search_inside",
        ),
        # "search [my] [type] for <query> [in <location>]"
        # e.g. "search my PDFs for machine learning", "search my excel files for revenue", "search my presentations for project architecture"
        (
            r"^(?:please\s+)?search\s+(?:my\s+)?(pdfs?|pdf\s+files?|word\s+documents?|docx\s+files?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?|documents?|notes?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?\s+for\s+(.+)$",
            "search_type_for",
        ),
        # "search [file contents / inside files] [in <location>] for <query>"
        (
            r"^(?:please\s+)?search\s+(?:file\s+contents|inside\s+files?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?\s+for\s+(.+)$",
            "search_for",
        ),
        # "which <type> contains [information about] <query> [in <location>]"
        # e.g. "Which PDF contains information about botnet detection?", "Which Word document contains AI Maze Challenge?"
        (
            r"^(?:please\s+)?which\s+(files?|pdfs?|word\s+documents?|docx(?:\s+files?)?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?|documents?)\s+(?:contains?|have|has)(?:\s+information\s+about|\s+info\s+about|\s+details\s+about|\s+the\s+words?)?\s+(.+?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?\??$",
            "which_type",
        ),
        # "find/show/locate (the)? <type> mentioning/discussing <query> [in <location>]"
        # e.g. "Find documents mentioning Docker Compose", "Find PowerPoint files mentioning project architecture"
        (
            r"^(?:please\s+)?(?:find|show\s+me|locate|list)\s+(?:the\s+)?(documents?|files?|notes?|pdfs?|pdf\s+files?|word\s+documents?|docx(?:\s+files?)?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?)\s+(?:mentioning|discussing|referencing)\s+(.+?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?$",
            "mentioning_type",
        ),
        # "find/search (the)? <type> that mention/contain <query> [in <location>]"
        (
            r"^(?:please\s+)?(?:find|show\s+me|locate|list)\s+(?:the\s+)?(documents?|files?|notes?|pdfs?|pdf\s+files?|word\s+documents?|docx(?:\s+files?)?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?)\s+that\s+(?:mentions?|contains?)\s+(.+?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?$",
            "that_contain_type",
        ),
        # "find/search files/documents [in <location>] containing <query>"
        (
            r"^(?:please\s+)?(?:find|show\s+me|locate|list)\s+(?:the\s+)?(files?|documents?|notes?|pdfs?|pdf\s+files?|word\s+documents?|docx(?:\s+files?)?|excel(?:\s+files?)?|spreadsheets?|powerpoint(?:\s+files?)?|presentations?|slides?)(?:\s+(?:in|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?)?\s+containing\s+(.+)$",
            "containing_type",
        ),
    ]

    DOC_TYPE_EXTENSIONS: dict[str, list[str]] = {
        "pdf": [".pdf"],
        "pdfs": [".pdf"],
        "pdf files": [".pdf"],
        "word": [".docx", ".doc"],
        "word document": [".docx", ".doc"],
        "word documents": [".docx", ".doc"],
        "docx": [".docx"],
        "docx files": [".docx"],
        "excel": [".xlsx", ".xls"],
        "excel files": [".xlsx", ".xls"],
        "spreadsheet": [".xlsx", ".xls", ".csv"],
        "spreadsheets": [".xlsx", ".xls", ".csv"],
        "powerpoint": [".pptx", ".ppt"],
        "powerpoint files": [".pptx", ".ppt"],
        "presentation": [".pptx", ".ppt"],
        "presentations": [".pptx", ".ppt"],
        "slide": [".pptx", ".ppt"],
        "slides": [".pptx", ".ppt"],
        "document": [".pdf", ".docx", ".doc", ".txt", ".md", ".rtf"],
        "documents": [".pdf", ".docx", ".doc", ".txt", ".md", ".rtf"],
        "text files": [".txt", ".py", ".md", ".json", ".csv", ".log"],
        "text file": [".txt", ".py", ".md", ".json", ".csv", ".log"],
    }

    for c_pat, pat_type in content_patterns:
        m_content = re.match(c_pat, cleaned, re.IGNORECASE)
        if m_content:
            c_loc = None
            c_exts = None
            c_query = None

            if pat_type == "search_inside":
                type_g = m_content.group(1)
                loc_g = m_content.group(2)
                q_g = m_content.group(3)
                c_loc = loc_g.strip() if loc_g else None
                c_query = q_g.strip().strip("'\"") if q_g else None
                if type_g:
                    tg = type_g.lower()
                    if tg == "text":
                        c_exts = [".txt", ".py", ".md", ".json", ".csv", ".log"]
                    elif tg in DOC_TYPE_EXTENSIONS:
                        c_exts = DOC_TYPE_EXTENSIONS[tg]
            elif pat_type == "search_type_for":
                type_g = m_content.group(1)
                loc_g = m_content.group(2)
                q_g = m_content.group(3)
                c_loc = loc_g.strip() if loc_g else None
                c_query = q_g.strip().strip("'\"") if q_g else None
                if type_g:
                    tg = type_g.lower()
                    if tg in DOC_TYPE_EXTENSIONS:
                        c_exts = DOC_TYPE_EXTENSIONS[tg]
            elif pat_type == "search_for":
                loc_g = m_content.group(1)
                q_g = m_content.group(2)
                c_loc = loc_g.strip() if loc_g else None
                c_query = q_g.strip().strip("'\"") if q_g else None
            elif pat_type in {"which_type", "mentioning_type", "that_contain_type"}:
                type_g = m_content.group(1)
                q_g = m_content.group(2)
                loc_g = m_content.group(3)
                c_query = q_g.strip().strip("'\"") if q_g else None
                c_loc = loc_g.strip() if loc_g else None
                if type_g:
                    tg = type_g.lower()
                    if tg in DOC_TYPE_EXTENSIONS:
                        c_exts = DOC_TYPE_EXTENSIONS[tg]
            elif pat_type == "containing_type":
                type_g = m_content.group(1)
                loc_g = m_content.group(2)
                q_g = m_content.group(3)
                c_loc = loc_g.strip() if loc_g else None
                c_query = q_g.strip().strip("'\"") if q_g else None
                if type_g:
                    tg = type_g.lower()
                    if tg in DOC_TYPE_EXTENSIONS:
                        c_exts = DOC_TYPE_EXTENSIONS[tg]

            if c_query:
                c_query = c_query.rstrip("?.!").strip()
                return {
                    "query": c_query,
                    "extension": None,
                    "extensions": c_exts,
                    "location": c_loc,
                    "mode": "content_search",
                    "sort_by": None,
                    "sort_order": None,
                    "time_filter": None,
                    "size_filter": None,
                }

    # Patterns for location-only directory listing (Non-recursive):
    # "what files are on my Desktop", "list files in Downloads", "show me files in Documents"
    # "show what's in Downloads", "what is in my Desktop folder", "what's in Downloads"
    listing_patterns = [
        r"^(?:please\s+)?(?:list|show(?:\s+me)?)\s+(?:the\s+)?files?\s+(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$",
        r"^(?:please\s+)?(?:show|tell\s+me)\s+(?:what(?:'s|\s+is))\s+(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$",
        r"^(?:please\s+)?what\s+files\s+(?:are\s+)?(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?\??$",
        r"^(?:please\s+)?what(?:\s+is|'s)\s+(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?\??$",
        r"^(?:please\s+)?list\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?\s+files?$",
    ]
    for l_pat in listing_patterns:
        m_list = re.match(l_pat, cleaned, re.IGNORECASE)
        if m_list:
            loc = m_list.group(1).strip().lower()
            return {
                "query": None,
                "extension": None,
                "location": loc,
                "mode": "list",
            }

    # Supported search action verbs: find, show me, list, look for, search for files
    # Match patterns like:
    # "find files in downloads"
    # "find my python files"
    # "find pdf files in downloads"
    # "find report.pdf"
    # "find files containing JARVIS"
    # "show me files in my Downloads folder"
    prefix_match = re.match(
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?(find|show\s+me|list|search(?:\s+for)?|locate)\s+(.*)$",
        cleaned,
        re.IGNORECASE,
    )

    rest = ""
    is_explicit_prefix = False

    if prefix_match:
        matched_verb = prefix_match.group(1).lower().strip()
        rest = prefix_match.group(2).rstrip("?.!").strip()
        is_explicit_prefix = True

        # If the verb is generic "search" or "search for", require file/folder context
        # (e.g. "search for python files", "search for report.pdf", "search for files in downloads")
        # to prevent generic web queries like "search for '; rm -rf /'" or "search for python tutorials"
        # from being misclassified as file discovery.
        if matched_verb in {"search", "search for"}:
            has_file_context = bool(
                re.search(r"\b(?:files?|documents?|notes?|pdfs?|spreadsheets?|presentations?|slides?|downloads|documents|desktop)\b", rest, re.I)
                or re.search(r"\.[a-zA-Z0-9]+$", rest)
            )
            if not has_file_context:
                return None
    else:
        # Check for natural filename + location without "find" (Bug 2)
        # e.g. "Homemade Indian Food Menu.png in Downloads"
        # e.g. "report.pdf in Downloads", "resume.pdf in Documents", "notes.txt on Desktop"
        file_loc_match = re.match(
            r"^([a-zA-Z0-9_\s.-]+\.[a-zA-Z0-9]+)\s+(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$",
            cleaned,
            re.IGNORECASE,
        )
        if file_loc_match:
            candidate_file = file_loc_match.group(1).strip()
            candidate_loc = file_loc_match.group(2).strip().lower()
            return {
                "query": candidate_file,
                "extension": None,
                "location": candidate_loc,
                "mode": "search",
            }

        return None

    if not rest:
        return None

    # If it's a web search (e.g. "find the best python tutorial online", "search for Python tutorials", "search the web for presentations"), ignore here
    if re.search(r"\b(online|on\s+the\s+web|the\s+web|google|bing|youtube|internet|tutorial|tutorials)\b", rest, re.IGNORECASE):
        return None

    location: str | None = None
    extension: str | None = None
    extensions: list[str] | None = None
    query: str | None = None
    sort_by: str | None = None
    sort_order: str | None = None
    time_filter: str | None = None
    size_filter: dict | None = None

    # Check for size filters (e.g. "larger than 5 MB", "smaller than 100 KB", "over 1 GB", "under 10 MB", "less than 2 MB")
    size_match = re.search(
        r"\b(?:(larger|bigger|greater|more)\s+than|over)\s+([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|bytes?|kilobytes?|megabytes?|gigabytes?)\b",
        rest,
        re.IGNORECASE,
    )
    if size_match:
        num = size_match.group(2)
        unit = size_match.group(3)
        b = parse_size_to_bytes(num, unit)
        if b is not None:
            size_filter = {"operator": "gt", "bytes": b}
            rest = rest[:size_match.start()] + " " + rest[size_match.end():]

    size_match_lt = re.search(
        r"\b(?:(smaller|less)\s+than|under)\s+([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|bytes?|kilobytes?|megabytes?|gigabytes?)\b",
        rest,
        re.IGNORECASE,
    )
    if size_match_lt:
        num = size_match_lt.group(2)
        unit = size_match_lt.group(3)
        b = parse_size_to_bytes(num, unit)
        if b is not None:
            size_filter = {"operator": "lt", "bytes": b}
            rest = rest[:size_match_lt.start()] + " " + rest[size_match_lt.end():]

    # Check for time filters (e.g. "modified today", "modified yesterday", "modified this week", "today", "yesterday", "this week")
    time_match = re.search(
        r"\b(?:modified\s+|created\s+)?(today|yesterday|this\s+week)\b",
        rest,
        re.IGNORECASE,
    )
    if time_match:
        t_term = time_match.group(1).lower().strip()
        if "week" in t_term:
            time_filter = "this_week"
        elif "yesterday" in t_term:
            time_filter = "yesterday"
        elif "today" in t_term:
            time_filter = "today"
        rest = rest[:time_match.start()] + " " + rest[time_match.end():]

    # Check for sorting (e.g. "newest", "latest", "oldest", "largest", "biggest", "smallest")
    sort_match = re.search(
        r"\b(?:the\s+)?(newest|latest|oldest|largest|biggest|smallest)\b",
        rest,
        re.IGNORECASE,
    )
    if sort_match:
        s_term = sort_match.group(1).lower().strip()
        if s_term in {"newest", "latest"}:
            sort_by = "modified_time"
            sort_order = "desc"
        elif s_term == "oldest":
            sort_by = "modified_time"
            sort_order = "asc"
        elif s_term in {"largest", "biggest"}:
            sort_by = "size"
            sort_order = "desc"
        elif s_term == "smallest":
            sort_by = "size"
            sort_order = "asc"
        rest = rest[:sort_match.start()] + " " + rest[sort_match.end():]

    rest = " ".join(rest.split())

    # Check for location at the end or inside: "in <location>", "in my <location> folder", "on <location>"
    loc_match = re.search(
        r"\s+(?:in|on|inside|under)\s+(?:my\s+)?([a-zA-Z0-9_\\/:-]+)(?:\s+folder|\s+directory)?$",
        rest,
        re.IGNORECASE,
    )
    if loc_match:
        location = loc_match.group(1).strip()
        rest = rest[:loc_match.start()].strip()
    else:
        # Also check mid-sentence location e.g. "in Downloads modified today"
        loc_mid_match = re.search(
            r"\b(?:in|on|inside|under)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?\b",
            rest,
            re.IGNORECASE,
        )
        if loc_mid_match:
            location = loc_mid_match.group(1).strip()
            rest = rest[:loc_mid_match.start()] + " " + rest[loc_mid_match.end():]

    # Check if rest begins with "my <location> files" or "<location> files" (e.g. "list my desktop files")
    loc_prefix_match = re.match(
        r"^(?:my\s+)?(desktop|documents|downloads)\s+files?$",
        rest,
        re.IGNORECASE,
    )
    if loc_prefix_match:
        location = loc_prefix_match.group(1).strip()
        rest = ""

    # Check for "files containing <query>" or "files with <query>"
    containing_match = re.search(r"\bfiles?\s+(?:containing|with|named|called)\s+(.+)$", rest, re.IGNORECASE)
    if containing_match:
        query = containing_match.group(1).strip().strip("'\"")
        rest = rest[:containing_match.start()].strip()

    # Check for extensions: e.g. "Python files", "PDF files", ".py files", ".txt"
    rest_norm = rest.lower().strip()
    # Check "my <type> files" or "<type> files"
    ext_match = re.match(r"^(?:my\s+)?([a-z0-9_.-]+)\s+files?$", rest_norm)
    if ext_match:
        ext_candidate = ext_match.group(1)
        if ext_candidate in COMMON_FILE_EXTENSIONS:
            val = COMMON_FILE_EXTENSIONS[ext_candidate]
            if isinstance(val, list):
                extensions = val
            else:
                extension = val
            rest = ""
        elif ext_candidate.startswith("."):
            extension = ext_candidate
            rest = ""
        else:
            query = ext_candidate
            rest = ""
    elif rest_norm in COMMON_FILE_EXTENSIONS:
        val = COMMON_FILE_EXTENSIONS[rest_norm]
        if isinstance(val, list):
            extensions = val
        else:
            extension = val
        rest = ""
    elif rest_norm.startswith((".", "*.")):
        extension = rest_norm.lstrip("*")
        rest = ""
    elif rest_norm in {"files", "my files", "all files", "files are", "files are on", "files are in", "the files", "the"}:
        rest = ""
    elif rest:
        # Check if rest looks like a filename (e.g. "report.pdf", "jarvis.py", "test", "Homemade Indian Food", "my resume")
        clean_rest = rest.strip().strip("'\"")
        # Strip leading conversational fillers such as "my " or "the " while preserving original casing
        m_filler = re.match(r"^(?:my|the|a|an)\s+(.+)$", clean_rest, re.IGNORECASE)
        if m_filler:
            clean_rest = m_filler.group(1).strip()
        query = clean_rest

    # If we extracted at least a location, extension, extensions, size, time, sort, or query, and user used file verbs
    has_any_criteria = (
        location is not None
        or extension is not None
        or extensions is not None
        or query is not None
        or size_filter is not None
        or time_filter is not None
        or sort_by is not None
        or rest_norm in {"files", "my files", "all files", ""}
    )
    if has_any_criteria:
        # If no query, no extension, no sort, no time, no size, and location is present -> directory listing
        mode = "list" if (
            query is None
            and extension is None
            and extensions is None
            and size_filter is None
            and time_filter is None
            and sort_by is None
            and location is not None
        ) else "search"

        return {
            "query": query,
            "extension": extension,
            "extensions": extensions,
            "location": location,
            "mode": mode,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "time_filter": time_filter,
            "size_filter": size_filter,
        }

    return None


def extract_followup_file_search_clarification(text: str) -> str | None:
    """
    Extract filename clarification from a follow-up user turn
    (e.g. 'Its name is Akhil_Resume(Updated) can you find it',
          'It is named report_final.pdf, can you search for it?',
          'named Akhil Resume').
    Returns the clarified filename string or None.
    Never matches prompt injections, deletions, or destructive requests.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Injections or deletions must NOT be handled as file follow-up
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, norm):
            return None

    # Match follow-up patterns
    pat = (
        r"^(?:please\s+)?"
        r"(?:its\s+name\s+is|it(?:\'s|\s+is)\s+named|named|called)\s+"
        r"([a-zA-Z0-9_\(\)\s.-]+?)"
        r"(?:(?:\s*,\s*|\s+)(?:can\s+you\s+|could\s+you\s+)?(?:find\s+it|look\s+for\s+it|search\s+for\s+it))?\??$"
    )
    m = re.match(pat, cleaned, re.IGNORECASE)
    if m:
        target_name = m.group(1).strip().strip("'\"")
        if target_name and len(target_name) > 1:
            return target_name
    return None


def extract_explicit_search_query(text: str) -> str | None:
    """
    Deterministically extract the literal search query from user text.
    Preserves all characters (quotes, semicolons, ampersands, slashes).
    Returns None if text is not an explicit search command or if query is empty.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Do not treat prompt injections as search commands
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Prefixes ordered from most specific to least specific
    prefixes = [
        r"^(?:please\s+)?open\s+(?:chrome|google\s+chrome|browser|edge|msedge)\s+and\s+search\s+for\s+",
        r"^(?:please\s+)?open\s+(?:chrome|google\s+chrome|browser|edge|msedge)\s+and\s+search\s+",
        r"^(?:please\s+)?open\s+(?:chrome|google\s+chrome|browser|edge|msedge)\s+and\s+look\s+up\s+",
        r"^(?:please\s+)?open\s+(?:chrome|google\s+chrome|browser|edge|msedge)\s+and\s+google\s+",
        r"^(?:please\s+)?search\s+google\s+for\s+",
        r"^(?:please\s+)?search\s+the\s+web\s+for\s+",
        r"^(?:please\s+)?search\s+online\s+for\s+",
        r"^(?:please\s+)?search\s+bing\s+for\s+",
        r"^(?:please\s+)?search\s+internet\s+for\s+",
        r"^(?:please\s+)?search\s+for\s+",
        r"^(?:please\s+)?look\s+up\s+",
        r"^(?:please\s+)?google\s+",
        r"^(?:please\s+)?search\s+",
    ]

    # Pre-check: if this is explicitly a local file content search, local file listing, or local document search,
    # it is NOT a browser search!
    # Strong local indicators (Bug 1 & 2 fix): "my presentations", "my pdfs", "my slides", "my documents", "in downloads", etc.
    LOCAL_INDICATORS_PATTERN = (
        r"\b(?:"
        r"my\s+(?:files?|pdfs?|documents?|presentations?|slides?|notes?|excel|spreadsheets?|word|powerpoint)"
        r"|inside\s+my"
        r"|in\s+my"
        r"|on\s+my"
        r"|in\s+(?:downloads|documents|desktop)"
        r"|on\s+(?:downloads|documents|desktop)"
        r"|files?\s+(?:in|on|under)"
        r"|which\s+(?:file|pdf|document|presentation|spreadsheet|word)"
        r"|(?:find|search)\s+(?:files?|documents?|notes?)\s+containing"
        r")\b"
    )

    # Explicit web indicators that override local indicators if present (e.g. "Search Google for PDFs about Docker")
    EXPLICIT_WEB_INDICATORS = r"\b(?:google|the\s+web|online|bing|internet)\b"

    has_web_indicator = bool(re.search(EXPLICIT_WEB_INDICATORS, norm))
    has_local_indicator = bool(re.search(LOCAL_INDICATORS_PATTERN, norm))

    if has_local_indicator and not has_web_indicator:
        return None

    # Check for "look up <query> online" pattern
    m_online = re.match(r"^(?:please\s+)?look\s+up\s+(.+?)\s+online$", cleaned, re.IGNORECASE)
    if m_online:
        q_online = m_online.group(1).strip()
        if q_online:
            return q_online

    for pat in prefixes:
        m = re.match(pat, cleaned, re.IGNORECASE)
        if m:
            query = cleaned[m.end():].strip()
            # If query is non-empty, return it preserving all exact characters
            if query:
                return query
            return None

    return None


def extract_explicit_open_file_target(text: str) -> dict[str, object] | None:
    """
    Deterministically detect requests to open a local file or select from recent search results.
    Examples:
    - Open my resume
    - Open resume.pdf
    - Open Akhil_Resume(Updated).pdf
    - Open that file / Open that PDF / Open that document / Can you open it?
    - Open the first result / Open result 2 / Open the second one
    Returns a dict with:
      {"type": "index", "index": int (1-based)} or
      {"type": "pronoun", "target": "it" | "pdf" | "document" | "file"} or
      {"type": "filename", "filename": str} or
      None
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Prompt injections / deletions / missing inputs are excluded
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, norm):
            return None

    # Exclude conversational open phrases
    if any(re.search(pat, norm) for pat in CONVERSATIONAL_OPEN_PATTERNS):
        return None

    # Exclude combined web searches
    if extract_explicit_search_query(text) is not None:
        return None

    # 1. Numbered result selection:
    # "5", "#5", "open 5", "open #5", "result 5", "open result 5", "open 13",
    # "Open the first result", "Open result 2", "Open the 2nd file", "open the first one",
    # "open number 5", "open result number 5", "open the 5th one", "open fifth one", "open the fifth result"
    WORD_TO_NUM = {
        "first": 1, "1st": 1,
        "second": 2, "2nd": 2,
        "third": 3, "3rd": 3,
        "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5,
        "sixth": 6, "6th": 6,
        "seventh": 7, "7th": 7,
        "eighth": 8, "8th": 8,
        "ninth": 9, "9th": 9,
        "tenth": 10, "10th": 10,
        "eleventh": 11, "11th": 11,
        "twelfth": 12, "12th": 12,
        "thirteenth": 13, "13th": 13,
        "fourteenth": 14, "14th": 14,
        "fifteenth": 15, "15th": 15,
        "sixteenth": 16, "16th": 16,
        "seventeenth": 17, "17th": 17,
        "eighteenth": 18, "18th": 18,
        "nineteenth": 19, "19th": 19,
        "twentieth": 20, "20th": 20,
    }

    # Bare number or #N: "5", "#5", "5.", "13", "#14"
    m_bare = re.match(r"^#?(\d+)\.?$", cleaned)
    if m_bare:
        return {"type": "index", "index": int(m_bare.group(1))}

    # Bare ordinal: "first", "second", "third", "fifth", "the first", "the fifth"
    m_bare_ord = re.match(r"^(?:the\s+)?([a-z]+)$", cleaned, re.IGNORECASE)
    if m_bare_ord and m_bare_ord.group(1).lower() in WORD_TO_NUM:
        return {"type": "index", "index": WORD_TO_NUM[m_bare_ord.group(1).lower()]}

    # "result 5", "#result 5", "result #5", "number 5", "result number 5"
    m_res = re.match(r"^(?:result\s+(?:number\s+)?#?|number\s+#?|#\s*)(\d+)$", cleaned, re.IGNORECASE)
    if m_res:
        return {"type": "index", "index": int(m_res.group(1))}

    words_alt = "|".join(WORD_TO_NUM.keys())
    num_pat = (
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
        r"(?:open|launch)\s+(?:the\s+)?"
        rf"(?:result\s+(?:number\s+)?#?\s*(\d+)|number\s+#?\s*(\d+)|#\s*(\d+)|(\d+)(?:st|nd|rd|th)?(?:\s+(?:result|file|document|one))?|"
        rf"({words_alt})(?:\s+(?:result|file|document|one))?|"
        r"([a-z0-9]+)\s+(?:result|file|document|one))\??$"
    )
    m_num = re.match(num_pat, cleaned, re.IGNORECASE)
    if m_num:
        idx = None
        if m_num.group(1):
            idx = int(m_num.group(1))
        elif m_num.group(2):
            idx = int(m_num.group(2))
        elif m_num.group(3):
            idx = int(m_num.group(3))
        elif m_num.group(4):
            idx = int(m_num.group(4))
        elif m_num.group(5) and m_num.group(5).lower() in WORD_TO_NUM:
            idx = WORD_TO_NUM.get(m_num.group(5).lower())
        elif m_num.group(6) and m_num.group(6).lower() in WORD_TO_NUM:
            idx = WORD_TO_NUM.get(m_num.group(6).lower())
        if idx is not None:
            return {"type": "index", "index": idx}

    # 2. Relative navigation reference (Milestone 4C.2):
    # "Open next", "Open the next one", "Open previous", "Open the previous one",
    # "Open the next file", "Open the previous file", "Open the next PDF", "Open the previous PDF",
    # "Open the one after that", "Open the one before that", "Open the one after this", "Open the one before this"
    rel_pat = (
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
        r"(?:open|launch)\s+"
        r"(?:the\s+)?"
        r"(next(?:\s+(?:one|file|pdf|document|result))?|"
        r"previous(?:\s+(?:one|file|pdf|document|result))?|"
        r"one\s+after\s+(?:that|this)|"
        r"one\s+before\s+(?:that|this))\??$"
    )
    m_rel = re.match(rel_pat, cleaned, re.IGNORECASE)
    if m_rel:
        rel_str = m_rel.group(1).lower()
        if "previous" in rel_str or "before" in rel_str:
            direction = "previous"
        else:
            direction = "next"
        # Extract target type if specified (e.g. "next pdf")
        target_type = "file"
        if "pdf" in rel_str:
            target_type = "pdf"
        elif "document" in rel_str:
            target_type = "document"
        return {"type": "relative", "direction": direction, "target": target_type}

    # 3. Pronoun / contextual reference:
    # "Open it", "Can you open it?", "Open that file", "Open that PDF", "Open that document", "Open it again"
    pronoun_pat = (
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
        r"(?:open|launch)\s+"
        r"(it|that\s+file|this\s+file|the\s+file|that\s+one|this\s+one|that\s+pdf|this\s+pdf|the\s+pdf|that\s+document|this\s+document|the\s+document)"
        r"(?:\s+again)?\??$"
    )
    m_pro = re.match(pronoun_pat, cleaned, re.IGNORECASE)
    if m_pro:
        ref = m_pro.group(1).lower()
        is_again = "again" in cleaned.lower()
        if "pdf" in ref:
            ptype = "pdf"
        elif "document" in ref:
            ptype = "document"
        elif "file" in ref:
            ptype = "file"
        else:
            ptype = "it"
        res_dict = {"type": "pronoun", "target": ptype}
        if is_again:
            res_dict["again"] = True
        return res_dict

    # 3. Explicit filename or path target:
    # "Open resume.pdf", "Open Akhil_Resume(Updated).pdf", "Open my resume", "Open C:\...\file.txt",
    # "Open the updated one", "Open the PDF one", "Open the resume called Akhil", "Open the updated resume"
    open_file_pat = (
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
        r"(?:open|launch)\s+"
        r"(?:the\s+file\s+(?:named|called)\s+|file\s+)?"
        r"([a-zA-Z0-9_\(\)\s.:/\\-]+?)\??$"
    )
    m_file = re.match(open_file_pat, cleaned, re.IGNORECASE)
    if m_file:
        target = m_file.group(1).strip().strip("'\"")
        target_lower = target.lower()

        # Reject application names from allowlist or common apps
        # (Must let open_application handle those!)
        from tools.application import resolve_application
        if resolve_application(target) is not None:
            return None

        # Reject web URLs or website aliases (Must let browser_navigate handle those!)
        from tools.browser import validate_and_normalize_url, SAFE_WEBSITE_ALIASES
        if target_lower in SAFE_WEBSITE_ALIASES or target_lower.startswith(("http://", "https://", "www.")):
            return None
        norm_url, _ = validate_and_normalize_url(target)
        if norm_url is not None:
            return None

        # Exclude common conversational or app words, and safe folder names
        if target_lower in {"it", "this", "that", "something", "up", "to", "a", "the", "my", "code", "file", "files", "browser", "application", "window", "next", "previous", "desktop", "documents", "downloads"}:
            return None

        # Exclude phrases that refer to folders (e.g. "my documents", "my downloads", "desktop folder", "downloads folder")
        norm_no_filler = re.sub(r"^(?:the|my|a|an)\s+", "", target_lower).strip()
        if re.match(r"^(desktop|documents|downloads)(?:\s+folder|\s+directory)?$", norm_no_filler):
            return None

        # Check for descriptive references like "the updated one", "the PDF one", "the new one"
        m_desc_one = re.match(r"^(?:the\s+)?(.+?)\s+one$", target, re.IGNORECASE)
        if m_desc_one:
            desc_val = m_desc_one.group(1).strip()
            # Ensure descriptor isn't a pure pronoun
            if desc_val.lower() not in {"this", "that", "the"}:
                return {"type": "descriptive", "description": desc_val}

        # If it has a file extension or has file-like descriptors (e.g. "my resume", "report.pdf", "test.py", "notes")
        has_ext = bool(re.search(r"\.[a-zA-Z0-9]+$", target))
        has_file_descriptor = target_lower.startswith("my ") or any(kw in target_lower for kw in [
            "resume", "cv", "pdf", "doc", "document", "spreadsheet", "presentation", "report", "notes", "updated", "called", "named"
        ])

        if has_ext or has_file_descriptor or "/" in target or "\\" in target:
            return {"type": "filename", "filename": target}

    return None


def extract_conversational_file_action(text: str) -> dict[str, object] | None:
    """
    Deterministically detect conversational questions and safe actions referring to the
    most recently opened file context (Milestone 4C.3).
    Categories:
    - get_name: What is its name?, What's its name?, What is the file name?, Tell me the name of this file, What file is this?
    - get_type: What type of file is it?, What file type is this?, What is its extension?, What is the file type?
    - check_type: Is this a PDF?, Is it a PDF?, Is this a Word document?, Is it an Excel file?
    - get_location: Where is it?, Where is this file?, What folder is it in?, Where did you open it from?, Where is the file?
    - reveal_location: Show me where it is, Show me the file location, Open the folder containing it, Open its folder, Show this file in Explorer
    Returns structured dict or None.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().rstrip("?.!").split())

    # Prompt injection / dangerous checks
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, norm):
            return None

    # Exclude combined web searches
    if extract_explicit_search_query(text) is not None:
        return None

    # 1. Reveal location:
    # "Show me where it is", "Show me the file location", "Open the folder containing it",
    # "Open its folder", "Show this file in Explorer", "Reveal it in Explorer", "Show where it is"
    reveal_patterns = [
        r"^(?:please\s+)?show\s+(?:me\s+)?where\s+(?:it|this\s+file|the\s+file)\s+is$",
        r"^(?:please\s+)?show\s+(?:me\s+)?(?:the\s+)?file\s+location$",
        r"^(?:please\s+)?open\s+(?:the\s+)?folder\s+containing\s+(?:it|this|that|the\s+file)$",
        r"^(?:please\s+)?open\s+(?:its|the|this)\s+folder$",
        r"^(?:please\s+)?show\s+(?:this\s+file|it)\s+in\s+explorer$",
        r"^(?:please\s+)?reveal\s+(?:this\s+file|it)\s+in\s+explorer$",
        r"^(?:please\s+)?open\s+containing\s+folder$",
    ]
    if any(re.match(p, norm) for p in reveal_patterns):
        return {"action": "reveal_location"}

    # 2. File name:
    # "What is its name?", "What's its name?", "What is the file name?",
    # "Tell me the name of this file", "What file is this?", "What is the name of it?"
    name_patterns = [
        r"^(?:what\s+is|what\'s)\s+its\s+name$",
        r"^(?:what\s+is|what\'s)\s+(?:the\s+)?(?:file\s+name|filename)$",
        r"^(?:what\s+is|what\'s)\s+the\s+name\s+of\s+(?:it|this|this\s+file|the\s+file)$",
        r"^tell\s+me\s+(?:the\s+name\s+of\s+(?:this\s+file|it)|its\s+name)$",
        r"^what\s+file\s+is\s+(?:this|it)$",
    ]
    if any(re.match(p, norm) for p in name_patterns):
        return {"action": "get_name"}

    # 3. Check type (yes/no):
    # "Is this a PDF?", "Is it a PDF?", "Is this a Word document?", "Is it a spreadsheet?", "Is this an Excel file?"
    type_ext_map = {
        "pdf": ".pdf",
        "word": ".docx",
        "doc": ".docx",
        "docx": ".docx",
        "excel": ".xlsx",
        "spreadsheet": ".xlsx",
        "xlsx": ".xlsx",
        "xls": ".xlsx",
        "powerpoint": ".pptx",
        "presentation": ".pptx",
        "pptx": ".pptx",
        "text": ".txt",
        "txt": ".txt",
        "csv": ".csv",
        "python": ".py",
        "py": ".py",
        "markdown": ".md",
        "md": ".md",
    }
    m_check = re.match(r"^is\s+(?:this|it)\s+(?:a|an)\s+([a-zA-Z]+)(?:\s+(?:file|document))?$", norm)
    if m_check:
        t_word = m_check.group(1).lower()
        if t_word in type_ext_map:
            return {"action": "check_type", "expected_extension": type_ext_map[t_word], "type_name": t_word.upper()}

    # 4. File type:
    # "What type of file is it?", "What file type is this?", "What is its extension?",
    # "What is the file type?", "What is the extension of this file?", "What type is it?"
    type_patterns = [
        r"^what\s+type\s+of\s+file\s+is\s+(?:it|this)$",
        r"^what\s+file\s+type\s+is\s+(?:this|it)$",
        r"^(?:what\s+is|what\'s)\s+(?:the\s+)?file\s+type$",
        r"^what\s+type\s+is\s+(?:it|this)$",
        r"^(?:what\s+is|what\'s)\s+its\s+extension$",
        r"^(?:what\s+is|what\'s)\s+(?:the\s+)?extension(?:\s+of\s+(?:this\s+file|it))?$",
    ]
    if any(re.match(p, norm) for p in type_patterns):
        return {"action": "get_type"}

    # 5. File location:
    # "Where is it?", "Where is this file?", "What folder is it in?",
    # "Where did you open it from?", "Where is the file?", "What directory is it in?"
    loc_patterns = [
        r"^where\s+is\s+(?:it|this\s+file|the\s+file|this)$",
        r"^what\s+(?:folder|directory)\s+is\s+(?:it|this\s+file|the\s+file)\s+in$",
        r"^where\s+did\s+you\s+open\s+(?:it|this|this\s+file|the\s+file)\s+from$",
        r"^which\s+(?:folder|directory)\s+is\s+(?:it|this\s+file)\s+in$",
    ]
    if any(re.match(p, norm) for p in loc_patterns):
        return {"action": "get_location"}

    return None


def extract_explicit_open_folder_target(text: str) -> str | None:
    """
    Deterministically detect requests to open a supported user folder.
    Milestone 4D.1 - Safe Folder Opening.
    Examples:
    - Open Downloads
    - Open my Downloads folder
    - Open Documents
    - Open my Documents
    - Open Desktop
    - Show my Desktop folder
    Returns normalized folder name ('desktop' | 'documents' | 'downloads') or None.
    Excludes contextual file actions (e.g., 'Open its folder', 'Open the folder containing it')
    and arbitrary system paths.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().rstrip("?.!").split())

    # Injections or dangerous commands excluded
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, norm):
            return None

    # Exclude combined web searches
    if extract_explicit_search_query(text) is not None:
        return None

    # Exclude contextual file actions (e.g. open its folder, open the folder containing it)
    if extract_conversational_file_action(text) is not None:
        return None

    # Match folder open patterns:
    # (open|show|view|launch)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$
    m = re.match(
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(open|show(?:\s+me)?|view|launch)\s+(?:my\s+)?(desktop|documents|downloads)(?:\s+folder|\s+directory)?$",
        norm,
    )
    if m:
        folder_target = m.group(2).lower()
        if folder_target in {"desktop", "documents", "downloads"}:
            return folder_target

    return None


def extract_explicit_open_app_target(text: str) -> str | None:
    """
    If the text is an explicit command to open or launch an application,
    extract the application name.
    Returns None for non-application actions, ambiguous tasks, conversational text,
    or combined open-and-search commands (which are handled as browser searches).
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Exclude conversational open expressions
    if any(re.search(pat, norm) for pat in CONVERSATIONAL_OPEN_PATTERNS):
        return None

    # Check for prompt injection keywords
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # If this is a combined search command, it should be handled via browser_search only!
    if extract_explicit_search_query(text) is not None:
        return None

    # Check ambiguous deletion or missing input
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None

    for pat, _ in MISSING_INPUT_PATTERNS:
        if re.search(pat, norm):
            return None

    # Match open/launch/start/run <target>
    m = re.match(r"^(open|launch|start|run)\s+([a-zA-Z0-9_\s.-]+)$", cleaned, re.IGNORECASE)
    if m:
        verb = m.group(1).lower()
        target = m.group(2).strip().rstrip(".?!")
        target_norm = target.lower()

        # If verb is 'start' or 'run', ONLY allow targets that are plausibly applications
        # and definitely NOT actions/gerunds/files/code
        if target_norm.startswith(NON_APPLICATION_PREFIXES):
            return None

        # Exclude stop words or vague pronouns (unless explicitly an allowed application like 'code')
        from tools.application import resolve_application
        if target_norm in {"it", "this", "that", "something", "up", "to", "a", "the", "my", "file", "files"}:
            return None
        if target_norm == "code" and resolve_application("code") is None:
            return None

        # Exclude pure numbers, numbered results (e.g. "5", "#5", "result 5", "13", "55"), ordinal phrases, relative navigation, folder reveal targets, or standard folders
        norm_no_filler = re.sub(r"^(?:the|my|a|an)\s+", "", target_norm).strip()
        if (
            re.match(r"^#?\d+$", norm_no_filler)
            or re.match(r"^(?:result\s+(?:number\s+)?#?\s*\d+|number\s+#?\s*\d+|result\s*#?\s*\d+|#\s*\d+|\d+(?:st|nd|rd|th)?(?:\s+(?:result|file|document|one))?)", norm_no_filler)
            or any(norm_no_filler.startswith(f"{word} ") for word in ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"])
            or norm_no_filler in {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"}
            or norm_no_filler in {"next", "previous", "next one", "previous one", "next file", "previous file", "next pdf", "previous pdf", "next document", "previous document"}
            or re.match(r"^one\s+(?:after|before)\s+(?:that|this)$", norm_no_filler)
            or norm_no_filler in {"folder", "its folder", "this folder", "that folder", "the folder", "folder containing it", "the folder containing it", "containing folder"}
            or norm_no_filler.startswith("folder containing")
            or re.match(r"^(desktop|documents|downloads)(?:\s+folder|\s+directory)?$", norm_no_filler)
        ):
            return None

        # If verb is start/run, ensure target doesn't contain action words
        if verb in {"start", "run"} and any(w in target_norm for w in ["delete", "remove", "clean", "erase", "wipe", "format", "script", "code", "program"]):
            return None

        return target

    return None


def extract_explicit_close_app_target(text: str) -> str | None:
    """
    If the text is an explicit command to close, quit, or exit an application,
    extract the application name.
    Examples:
    - Close Chrome
    - Close Google Chrome
    - Quit Chrome
    - Exit Chrome
    - Close VS Code
    - Close Code
    - Exit Visual Studio Code
    - Close Calculator
    - Quit Calculator
    - Close Notepad
    - Exit Notepad
    Returns None for search queries, prompts, injections, or non-close commands.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Check for prompt injection keywords
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Exclude combined web searches (e.g. "Search for how to close Chrome")
    if extract_explicit_search_query(text) is not None:
        return None

    # Match close/quit/exit/kill/terminate <target>
    m = re.match(r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(close|quit|exit|kill|terminate)\s+([a-zA-Z0-9_\s.:/\\-]+?)\??$", cleaned, re.IGNORECASE)
    if m:
        target = m.group(2).strip().rstrip(".?!")
        target_norm = target.lower()

        # Exclude stop words or vague pronouns
        if target_norm in {"it", "this", "that", "something", "up", "to", "a", "the", "all", "window", "tab"}:
            return None

        # Exclude files, documents, downloads, etc. if user says "close file"
        if target_norm in {"file", "files", "document", "documents"}:
            return None

        return target

    return None


def extract_explicit_app_status_target(text: str) -> str | None:
    """
    If the text is an explicit query checking whether an application is running or open,
    extract the application name.
    Examples:
    - Is Chrome running?
    - Is Google Chrome open?
    - Is VS Code running?
    - Is Calculator open?
    - Is Notepad running?
    - Is Chrome currently running?
    - Is Chrome active?
    Returns application name or None.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Check for prompt injection keywords
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Exclude combined web searches
    if extract_explicit_search_query(text) is not None:
        return None

    # Match queries like "is <app> (currently | active | running | open)?"
    m = re.match(
        r"^(?:please\s+)?is\s+([a-zA-Z0-9_\s.-]+?)\s+(?:currently\s+)?(running|open|active)\??$",
        norm,
        re.IGNORECASE,
    )
    if m:
        target = m.group(1).strip()
        # Exclude pronouns, generic app words, files, general words
        if target in {
            "it", "this", "that", "the", "a", "my", "file", "folder",
            "that app", "the app", "this app", "that application", "the application", "this application", "app", "application"
        }:
            return None
        return target

    # Also match "check if <app> is (currently )?(running|open|active)?"
    m2 = re.match(
        r"^(?:please\s+)?(?:check\s+(?:if|whether)|see\s+if)\s+([a-zA-Z0-9_\s.-]+?)\s+(?:is\s+)?(?:currently\s+)?(running|open|active)\??$",
        norm,
        re.IGNORECASE,
    )
    if m2:
        target = m2.group(1).strip()
        if target in {
            "it", "this", "that", "the", "a", "my", "file", "folder",
            "that app", "the app", "this app", "that application", "the application", "this application", "app", "application"
        }:
            return None
        return target

    return None


def extract_conversational_application_action(text: str) -> dict[str, object] | None:
    """
    Detect conversational application lifecycle references (Milestone 4D.5) using pronouns:
    - 'it', 'that app', 'the app', 'that application', 'the application', 'this app', 'this application'
    
    Supported action types:
    1. 'check_application_running':
       - 'Is it running?', 'Is that app running?', 'Is the app open?',
       - 'Is the application currently running?', 'Is it open?', 'Is it active?'
       - 'Check if it is running', 'Check if that app is open'
    2. 'close_application':
       - 'Close it', 'Quit it', 'Exit it'
       - 'Close that app', 'Quit that app', 'Close the app'
       - 'Exit the application', 'Close that application', 'Quit the application'
    
    Returns:
      {'action': 'check_application_running', 'reference': str} or
      {'action': 'close_application', 'reference': str} or
      None
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().rstrip("?.!").split())

    # Check for prompt injection keywords
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Exclude combined web searches (e.g. "Search for how to close it", "Search for why Chrome is running slowly")
    if extract_explicit_search_query(text) is not None:
        return None

    # Exclude conversational file queries from Milestone 4C.3:
    # "What is its name?", "What type of file is it?", "Where is it?", "Show me where it is", "Open its folder"
    if extract_conversational_file_action(text) is not None:
        return None

    APP_PRONOUNS_PATTERN = r"(?:it|that\s+app|the\s+app|this\s+app|that\s+application|the\s+application|this\s+application)"

    # 1. Close application commands
    close_m = re.match(
        rf"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(close|quit|exit|kill|terminate)\s+({APP_PRONOUNS_PATTERN})$",
        norm,
        re.IGNORECASE,
    )
    if close_m:
        return {
            "action": "close_application",
            "reference": close_m.group(2).strip(),
        }

    # 2. Check running status queries
    # e.g. "is it running", "is that app open", "is the application currently running", "is it active"
    status_m1 = re.match(
        rf"^(?:please\s+)?is\s+({APP_PRONOUNS_PATTERN})\s+(?:currently\s+)?(running|open|active)$",
        norm,
        re.IGNORECASE,
    )
    if status_m1:
        return {
            "action": "check_application_running",
            "reference": status_m1.group(1).strip(),
        }

    # e.g. "check if it is running", "see if that app is open", "check if the app is currently running"
    status_m2 = re.match(
        rf"^(?:please\s+)?(?:check\s+(?:if|whether)|see\s+if)\s+({APP_PRONOUNS_PATTERN})\s+(?:is\s+)?(?:currently\s+)?(running|open|active)$",
        norm,
        re.IGNORECASE,
    )
    if status_m2:
        return {
            "action": "check_application_running",
            "reference": status_m2.group(1).strip(),
        }

    return None


def extract_browser_navigation_target(text: str) -> str | None:
    """
    Deterministically detect commands to open or navigate to a website or URL (Milestone 5.1).
    Examples:
    - Open Google
    - Open YouTube
    - Open GitHub
    - Open ChatGPT
    - Open Wikipedia
    - Open https://example.com
    - Go to google.com
    - Navigate to https://github.com
    - Go to https://example.com
    - Visit github.com
    
    Disambiguation rules:
    - Explicit searches ('Search for ...', 'Look up ...') are NOT website navigation.
    - Application names from APPLICATION_ALLOWLIST ('Open Chrome', 'Open VS Code', 'Open Calculator', 'Open Notepad', 'Open File Explorer')
      are NOT website navigation (they remain application launches).
    - Folders ('Open Downloads', 'Open Desktop') are NOT website navigation.
    - Files ('Open resume.pdf') are NOT website navigation.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Prompt injection check
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Exclude explicit browser search commands
    if extract_explicit_search_query(text) is not None:
        return None

    # Match "open/go to/navigate to/visit <target>"
    m = re.match(
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:open(?:\s+a\s+website|\s+the\s+website)?|go\s+to|navigate\s+to|visit)\s+([a-zA-Z0-9_.:/\\-]+?)\??$",
        cleaned,
        re.IGNORECASE,
    )
    if not m:
        # Also check bare URLs or "open a website" queries
        return None

    target = m.group(1).strip().strip("'\"")
    target_lower = target.lower()

    # Exclude applications from APPLICATION_ALLOWLIST (Chrome, VS Code, Notepad, Calculator, Explorer, etc.)
    from tools.application import resolve_application
    if resolve_application(target) is not None:
        return None

    # Exclude standard folders
    if target_lower in {"downloads", "documents", "desktop", "downloads folder", "documents folder", "desktop folder"}:
        return None

    # Exclude common conversational or file stop words
    if target_lower in {"it", "this", "that", "something", "up", "to", "a", "the", "my", "file", "files", "folder", "app", "application"}:
        return None

    # Validate against URL / alias validator
    from tools.browser import validate_and_normalize_url
    norm_url, err = validate_and_normalize_url(target)
    if norm_url and not err:
        return target

    return None


def extract_browser_control_action(text: str) -> dict[str, object] | None:
    """
    Deterministically detect browser control and page-awareness commands (Milestone 5.1).
    Supported actions:
    1. 'browser_go_back':
       - 'Go back', 'Back', 'Previous page'
    2. 'browser_go_forward':
       - 'Go forward', 'Forward', 'Next page'
    3. 'browser_refresh':
       - 'Refresh', 'Refresh the page', 'Reload', 'Reload the page'
    4. 'browser_get_page_info':
       - 'What page am I on?', 'What website is this?', 'Where am I?'
       - 'What is the title of this page?', 'What is this page called?' (type='title')
       - 'What URL am I on?', 'What is the URL?' (type='url')
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().rstrip("?.!").split())

    # Check for prompt injection keywords
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None

    # Exclude searches (e.g. "Search for how to go back in Chrome")
    if extract_explicit_search_query(text) is not None:
        return None

    # 1. Go back
    if norm in {"go back", "back", "previous page", "browser back", "go to previous page", "go back to previous page"}:
        return {"action": "browser_go_back"}

    # 2. Go forward
    if norm in {"go forward", "forward", "next page", "browser forward", "go to next page"}:
        return {"action": "browser_go_forward"}

    # 3. Refresh / reload
    if norm in {"refresh", "refresh the page", "refresh page", "reload", "reload the page", "reload page"}:
        return {"action": "browser_refresh"}

    # 4. Page title queries
    if norm in {
        "what is the title of this page",
        "what is this page called",
        "what is the page title",
        "what is the title",
        "what's the title of this page",
        "what's this page called",
        "tell me the title of this page",
        "tell me what this page is called",
    }:
        return {"action": "browser_get_page_info", "type": "title"}

    # 5. Page URL queries
    if norm in {
        "what url am i on",
        "what is the url",
        "what is the current url",
        "what's the url",
        "what is this url",
        "tell me the url",
        "what url is this",
    }:
        return {"action": "browser_get_page_info", "type": "url"}

    # 6. General page / website awareness queries
    if norm in {
        "what page am i on",
        "what website is this",
        "what site is this",
        "where am i",
        "what website am i on",
        "which page am i on",
        "tell me what page i am on",
        "tell me what website i am on",
    }:
        return {"action": "browser_get_page_info", "type": "page_info"}

    return None


def extract_single_command_action(segment: str) -> dict[str, object] | None:
    """
    Check if a text segment corresponds deterministically to a single supported action:
    - browser_search (search for ...)
    - open_folder (open downloads, open documents, open desktop)
    - open_application (open chrome, launch notepad, open calc, etc.)
    - open_file (open file, open resume.pdf, open 1, etc.)
    Returns a dictionary describing the tool and arguments, or None.
    """
    s = segment.strip()
    if not s:
        return None

    # 1. Check browser search
    # Note: If segment starts with "search for", "google", "look up", etc.
    q = extract_explicit_search_query(s)
    if q is not None:
        return {"tool": "browser_search", "arguments": {"query": q}}

    # 2. Check open folder
    f_target = extract_explicit_open_folder_target(s)
    if f_target is not None:
        return {"tool": "open_folder", "arguments": {"folder": f_target}}

    # 3. Check browser navigation (Milestone 5.1: websites & URLs take precedence before generic open app)
    nav_target = extract_browser_navigation_target(s)
    if nav_target is not None:
        from tools.browser import validate_and_normalize_url
        norm_u, _ = validate_and_normalize_url(nav_target)
        return {"tool": "browser_navigate", "arguments": {"url": norm_u or nav_target}}

    # 4. Check browser control action (Milestone 5.1: go back, forward, refresh, page info)
    ctrl_action = extract_browser_control_action(s)
    if ctrl_action is not None:
        act_tool = ctrl_action["action"]
        act_args = {}
        if "type" in ctrl_action:
            act_args["type"] = ctrl_action["type"]
        return {"tool": act_tool, "arguments": act_args}

    # 5. Check open application
    app_target = extract_explicit_open_app_target(s)
    if app_target is not None:
        from tools.application import resolve_application
        res_app = resolve_application(app_target)
        if res_app is not None:
            disp_name = res_app[1].get("display_name", res_app[0])
            return {"tool": "open_application", "arguments": {"application": disp_name}}
        else:
            return {"tool": "open_application", "arguments": {"application": app_target}}

    # 6. Check close application
    close_app_target = extract_explicit_close_app_target(s)
    if close_app_target is not None:
        from tools.application import resolve_application
        res_app = resolve_application(close_app_target)
        if res_app is not None:
            disp_name = res_app[1].get("display_name", res_app[0])
            return {"tool": "close_application", "arguments": {"application": disp_name}}
        else:
            return {"tool": "close_application", "arguments": {"application": close_app_target}}

    # 7. Check application status
    status_app_target = extract_explicit_app_status_target(s)
    if status_app_target is not None:
        from tools.application import resolve_application
        res_app = resolve_application(status_app_target)
        if res_app is not None:
            disp_name = res_app[1].get("display_name", res_app[0])
            return {"tool": "check_application_running", "arguments": {"application": disp_name}}
        else:
            return {"tool": "check_application_running", "arguments": {"application": status_app_target}}

    # 8. Check conversational application pronoun action (Milestone 4D.5)
    conv_app_action = extract_conversational_application_action(s)
    if conv_app_action is not None:
        action_name = conv_app_action["action"]
        return {"tool": action_name, "conversational_reference": conv_app_action["reference"]}

    # 9. Check open file
    file_target = extract_explicit_open_file_target(s)
    if file_target is not None:
        return {"tool": "open_file", "target_info": file_target}

    return None


def extract_multi_step_actions(text: str) -> list[dict[str, object]] | None:
    """
    Conservative multi-step detection for Milestone 4D.2.
    Detects commands joined by 'and then', 'then', 'after that', or 'and'.
    Ensures search queries with internal 'and' (e.g. 'Search for AI and machine learning')
    are NOT split.
    Only splits if EVERY segment resolves to a valid executable command.
    Returns list of action dicts if valid multi-step command, or None.
    """
    cleaned = text.strip()
    norm = " ".join(cleaned.lower().split())

    # Injections or dangerous commands excluded from multi-step splitting
    if any(re.search(pat, norm) for pat in INJECTION_PATTERNS):
        return None
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, norm):
            return None

    # Search query protection: If the entire input is already a search query that does NOT
    # start with a browser/folder command like "Open Chrome and search for ...", do NOT split!
    # E.g. "Search for AI and machine learning", "Search for Python and Java tutorials"
    if norm.startswith(("search ", "search for ", "google ", "look up ")):
        return None

    # Note: Search queries containing 'and' are protected above by the check on norm.startswith.
    unified_conn = r"\s+(?:and\s+then|after\s+that|then|and)\s+|\s*,\s*then\s+"
    parts = [p.strip() for p in re.split(unified_conn, cleaned, flags=re.IGNORECASE) if p.strip()]
    if len(parts) > 1:
        actions: list[dict[str, object]] = []
        all_valid = True
        for part in parts:
            act = extract_single_command_action(part)
            if act is None:
                all_valid = False
                break
            actions.append(act)

        if all_valid:
            return actions

    return None


def classify_intent(text: str) -> IntentCategory:
    """
    Classify user input following strict priority:
    1. Explicit continuation
    2. Prompt injection overrides (defensively caught before tasks or dangerous commands)
    3. Essential missing input (e.g. 'run this Python code' -> prompt for code, 'search for' -> prompt for query)
    4. Explicit web search commands (BUG 4 FIX: searches take precedence over dangerous keywords in query)
    5. Explicit memory commands (store, forget, long-term recall)
    6. Historical working-memory recall
    7. Ambiguous deletion requests (e.g. 'start deleting old files' -> clarify files & location)
    8. Dangerous tasks (local deletions, emails, financial)
    9. Unclear / vague requests
    10. Exact casual conversation matches
    11. Explicit current task (including open/launch <app>)
    12. Conversational question starters
    13. Normal conversation fallback
    """
    raw = text.strip()
    if not raw:
        return "unclear"

    # Normalize
    cleaned = re.sub(r"[^\w\s]", " ", raw.lower())
    normalized = " ".join(cleaned.split())

    # 1. Explicit continuation
    for pat in CONTINUATION_PATTERNS:
        if re.search(pat, normalized):
            return "continuation"

    # 2. Prompt injection overrides
    for pat in INJECTION_PATTERNS:
        if re.search(pat, normalized):
            return "prompt_injection"

    # 3. Essential missing input (e.g. "run this Python code", "search for")
    if check_missing_input(normalized):
        return "missing_input"

    # 4a. Explicit file discovery / content search commands (Precedence before generic web search)
    if extract_explicit_file_search_params(raw) is not None:
        return "task"

    # 4b. Explicit open file / result selection commands (Precedence before memory/conversation)
    if extract_explicit_open_file_target(raw) is not None:
        return "task"

    # 4c. Explicit open folder commands (Precedence before memory/conversation)
    if extract_explicit_open_folder_target(raw) is not None:
        return "task"

    # 4d. Explicit application lifecycle commands (close app / app status)
    if extract_explicit_close_app_target(raw) is not None:
        return "task"
    if extract_explicit_app_status_target(raw) is not None:
        return "task"

    # 4e. Conversational application lifecycle pronoun commands (Milestone 4D.5)
    if extract_conversational_application_action(raw) is not None:
        return "task"

    # 4f. Explicit web search commands (Precedence over dangerous keywords in query)
    if extract_explicit_search_query(raw) is not None:
        return "task"

    # 4g. Explicit browser navigation or control commands (Milestone 5.1)
    if extract_browser_navigation_target(raw) is not None:
        return "task"
    if extract_browser_control_action(raw) is not None:
        return "task"

    # 5. Explicit memory commands
    for pat in LONG_TERM_RECALL_PATTERNS:
        if re.search(pat, normalized):
            return "memory_recall"

    for pat in MEMORY_STORE_PATTERNS:
        if re.search(pat, normalized):
            return "memory_store"

    for pat in MEMORY_FORGET_PATTERNS:
        if re.search(pat, normalized):
            return "memory_forget"

    # 6. Memory recall / historical conversation questions
    if is_memory_recall_request(normalized):
        return "conversation"

    # 7. Ambiguous deletion requests (e.g. "start deleting old files")
    for pat in AMBIGUOUS_DELETION_PATTERNS:
        if re.search(pat, normalized):
            return "unclear"

    # 8. Dangerous task triggers (for actual local actions, e.g. "delete all files in my Downloads folder")
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, normalized):
            return "dangerous_task"

    # 9. Unclear / vague expressions
    for pat in UNCLEAR_PATTERNS:
        if re.search(pat, normalized):
            return "unclear"

    # 10. Exact casual conversation matches
    if normalized in EXACT_CONVERSATIONS:
        return "conversation"

    # 11. Check conversational open phrases (e.g. "open to suggestions")
    if any(re.search(pat, normalized) for pat in CONVERSATIONAL_OPEN_PATTERNS):
        return "conversation"

    # 12. Explicit task patterns
    for pat in TASK_PATTERNS:
        if re.search(pat, normalized):
            return "task"

    # 13. Conversational question starters
    for starter in CONVERSATIONAL_STARTERS:
        if normalized.startswith(starter):
            return "conversation"

    # 14. Single-word inputs that are not tasks
    if " " not in normalized and len(normalized) <= 4:
        return "conversation"

    # 15. Wh- questions without task verbs
    if normalized.startswith(("how", "what", "why", "who", "when", "where", "can you tell me")):
        return "conversation"

    # 16. Fallback for short unclassified strings
    word_count = len(normalized.split())
    if word_count <= 3 and not any(v in normalized for v in ["open", "search", "find", "read", "create", "draft", "delete", "send", "look"]):
        return "unclear"

    return "conversation"
