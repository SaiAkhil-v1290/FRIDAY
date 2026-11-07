import re
from dataclasses import dataclass
from memory.long_term_memory import LongTermMemory
from memory.models import Memory

MEMORY_STORE_PREFIXES = [
    r"^remember\s+that\s+",
    r"^remember\s+this\s*:\s*",
    r"^remember\s+this\s+",
    r"^remember\s+",
    r"^save\s+this\s*:\s*",
    r"^save\s+this\s+",
    r"^save\s+that\s*:\s*",
    r"^save\s+that\s+",
    r"^keep\s+in\s+mind\s+that\s+",
    r"^keep\s+in\s+mind\s+",
    r"^store\s+this\s*:\s*",
    r"^store\s+this\s+",
    r"^add\s+this\s+to\s+your\s+memory\s*:\s*",
    r"^add\s+this\s+to\s+your\s+memory\s+",
]

MEMORY_FORGET_PREFIXES = [
    r"^forget\s+that\s+",
    r"^forget\s+my\s+preference\s+about\s+",
    r"^forget\s+about\s+",
    r"^forget\s+",
    r"^remove\s+(that\s+from\s+your\s+memory|this\s+from\s+your\s+memory|that\s+memory|this\s+memory)$",
    r"^delete\s+(that\s+memory|this\s+memory)$",
    r"^remove\s+",
    r"^delete\s+memory\s+",
]

DANGEROUS_FORGET_PATTERNS = [
    r"^forget\s+everything$",
    r"^delete\s+all\s+memories$",
    r"^clear\s+my\s+memory$",
    r"^wipe\s+all\s+your\s+memory$",
    r"^wipe\s+memory$",
    r"^clear\s+all\s+memories$",
]


def extract_store_content(text: str) -> str:
    """Extract clean content from explicit store command."""
    cleaned = text.strip()
    # Check bare store prompts with no content
    norm = cleaned.lower().rstrip(".?!:")
    if norm in {"remember this", "remember that", "remember", "save this", "save that", "store this", "keep this in mind", "keep in mind"}:
        return ""

    for prefix in MEMORY_STORE_PREFIXES:
        m = re.match(prefix, cleaned, re.IGNORECASE)
        if m:
            extracted = cleaned[m.end():].strip()
            # Strip outer quotes if any
            if (extracted.startswith('"') and extracted.endswith('"')) or (extracted.startswith("'") and extracted.endswith("'")):
                extracted = extracted[1:-1].strip()
            # If extracted is just 'this' or empty
            if extracted.lower().rstrip(".?!:") in {"", "this", "that"}:
                return ""
            return extracted
    return ""


def extract_forget_target(text: str) -> str:
    """Extract target phrase from explicit forget command."""
    cleaned = text.strip()
    for prefix in MEMORY_FORGET_PREFIXES:
        m = re.match(prefix, cleaned, re.IGNORECASE)
        if m:
            extracted = cleaned[m.end():].strip()
            if (extracted.startswith('"') and extracted.endswith('"')) or (extracted.startswith("'") and extracted.endswith("'")):
                extracted = extracted[1:-1].strip()
            # Remove trailing punctuation
            extracted = re.sub(r"[.?!]+$", "", extracted).strip()
            return extracted
    return cleaned


def detect_memory_category(content: str) -> tuple[str, int]:
    """
    Deterministic basic category and importance selection:
    preference: prefer / preference / like / dislike
    project: project / building / developing / working on
    fact: name / identity / basic stable information
    other: default
    """
    c_lower = content.lower()
    if any(k in c_lower for k in ["prefer", "preference", "like", "dislike", "favorite"]):
        return "preference", 6
    if any(k in c_lower for k in ["project", "building", "developing", "working on", "working with"]):
        return "project", 7
    if any(k in c_lower for k in ["name is", "call me", "i am a", "i live in", "system is", "use windows", "use mac"]):
        return "fact", 7
    return "other", 5


@dataclass
class MemoryCommandResult:
    message: str
    success: bool = True
    memory: Memory | None = None


class MemoryCommandHandler:
    """Handles explicit store, forget, and recall operations against LongTermMemory."""

    def __init__(self, long_term_memory: LongTermMemory) -> None:
        self.ltm = long_term_memory

    def handle_store(self, text: str) -> MemoryCommandResult:
        content = extract_store_content(text)
        if not content:
            return MemoryCommandResult(
                message="Sure — what would you like me to remember?",
                success=False,
            )

        category, importance = detect_memory_category(content)

        # Check existing memories for duplicate check
        existing_memories = self.ltm.list_memories(category=category, limit=500)
        already_exists = any(m.content.lower() == content.lower() for m in existing_memories)

        mem = self.ltm.add_memory(content=content, category=category, importance=importance)

        if already_exists:
            return MemoryCommandResult(
                message="I already have that in my memory.",
                success=True,
                memory=mem,
            )

        # Grammatically safe confirmation without unsafe pronoun rewriting
        return MemoryCommandResult(
            message=f'Got it — I\'ll remember: "{content}"',
            success=True,
            memory=mem,
        )

    def handle_recall(self, text: str) -> MemoryCommandResult:
        normalized = text.strip().lower()

        # Check topic-specific recall
        if any(w in normalized for w in ["project", "projects"]):
            project_mems = self.ltm.list_memories(category="project", limit=10)
            if not project_mems:
                project_mems = self.ltm.search_memories("project", limit=10)
            if not project_mems:
                return MemoryCommandResult(
                    message="I don't have any memories saved about your projects yet.",
                    success=True,
                )
            lines = [f"• {m.content}" for m in project_mems]
            return MemoryCommandResult(
                message="Here is what I remember about your projects:\n\n" + "\n".join(lines),
                success=True,
            )

        if any(w in normalized for w in ["preference", "preferences"]):
            pref_mems = self.ltm.list_memories(category="preference", limit=10)
            prefs = self.ltm.list_preferences()
            items = [f"• {m.content}" for m in pref_mems]
            for p in prefs:
                items.append(f"• [{p.category}] {p.key}: {p.value}")
            if not items:
                return MemoryCommandResult(
                    message="I don't have any preferences saved yet.",
                    success=True,
                )
            return MemoryCommandResult(
                message="Here are your saved preferences:\n\n" + "\n".join(items),
                success=True,
            )

        # General recall
        all_mems = self.ltm.list_memories(limit=15)
        profiles = self.ltm.list_profile()
        prefs = self.ltm.list_preferences()

        if not all_mems and not profiles and not prefs:
            return MemoryCommandResult(
                message="I don't have any long-term memories saved yet.",
                success=True,
            )

        lines: list[str] = []
        for p in profiles:
            lines.append(f"• {p.key.replace('_', ' ').title()}: {p.value}")
        for p in prefs:
            lines.append(f"• Preference [{p.category}]: {p.key} = {p.value}")
        for m in all_mems:
            lines.append(f"• {m.content}")

        summary = "Here's what I currently remember:\n\n" + "\n".join(lines)
        return MemoryCommandResult(
            message=summary,
            success=True,
        )

    def handle_forget(self, text: str) -> MemoryCommandResult:
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())

        # Check dangerous bulk memory deletion
        for pat in DANGEROUS_FORGET_PATTERNS:
            if re.search(pat, normalized):
                return MemoryCommandResult(
                    message="Clearing all long-term memories is a destructive action. Please explicitly confirm the specific memories you want removed.",
                    success=False,
                )

        target = extract_forget_target(text)
        if not target:
            return MemoryCommandResult(
                message="Please specify which memory you would like me to forget.",
                success=False,
            )

        # Search matching memories
        candidates = self.ltm.search_memories(target, limit=10)

        # Filter exact or strong matches
        exact_matches = [m for m in candidates if m.content.lower().strip() == target.lower().strip()]
        if not exact_matches:
            # Check if target is a substring of content or vice versa
            exact_matches = [
                m for m in candidates
                if target.lower() in m.content.lower() or m.content.lower() in target.lower()
            ]

        if not exact_matches:
            return MemoryCommandResult(
                message="I couldn't find that memory in my long-term memory.",
                success=False,
            )

        if len(exact_matches) > 1:
            match_descriptions = "\n".join(f"• {m.content}" for m in exact_matches[:3])
            return MemoryCommandResult(
                message=(
                    "I found multiple memories that could match. Which one would you like me to forget?\n\n"
                    + match_descriptions
                ),
                success=False,
            )

        # Single exact match
        mem_to_delete = exact_matches[0]
        self.ltm.delete_memory(mem_to_delete.id)
        return MemoryCommandResult(
            message="I've forgotten that memory.",
            success=True,
            memory=mem_to_delete,
        )
