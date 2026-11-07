import re
from typing import Sequence
from memory.models import Memory

MEMORY_FACT_QUERY_PATTERNS = [
    r"\bwhat\s+do\s+you\s+know\s+about\b",
    r"\bwhat\s+do\s+you\s+remember\s+about\b",
    r"\bwhat\s+technology\s+does\b",
    r"\bwhat\s+technologies\s+does\b",
    r"\bwhat\s+tech\s+stack\b",
    r"\bwhat\s+language\s+does\b",
    r"\bwhat\s+programming\s+language\b",
    r"\bwhat\s+framework\s+does\b",
    r"\bwhat\s+database\s+does\b",
    r"\bwhat\s+libraries\s+does\b",
    r"\bwhat\s+tools\s+does\b",
    r"\bwhat\s+have\s+i\s+told\s+you\s+about\b",
    r"\btell\s+me\s+about\s+my\b",
    r"\bmy\s+preferences\b",
    r"\bwhat\s+do\s+i\s+prefer\b",
    r"\bwhat\s+is\s+my\s+(project|preference|name|favorite)\b",
]

SUGGESTION_QUERY_PATTERNS = [
    r"\bhow\s+can\s+i\s+improve\b",
    r"\bhow\s+to\s+improve\b",
    r"\bwhat\s+should\s+i\s+add\b",
    r"\bwhat\s+can\s+i\s+add\b",
    r"\bany\s+suggestions\b",
    r"\bany\s+recommendations\b",
    r"\bhow\s+should\s+i\s+build\b",
    r"\bhow\s+should\s+i\s+design\b",
    r"\bideas\s+for\b",
]

SPECIFIC_ASPECT_PATTERNS = {
    "database": [r"\bdatabase\b", r"\bdb\b", r"\bsql\b", r"\bsqlite\b", r"\bpostgres\b", r"\bmongo\b"],
    "frontend": [r"\bfrontend\b", r"\breact\b", r"\bui\b", r"\bvue\b", r"\bangular\b"],
    "backend": [r"\bbackend\b", r"\bserver\b", r"\bapi\b"],
    "framework": [r"\bframework\b", r"\bfastapi\b", r"\bdjango\b", r"\bflask\b"],
    "hosting": [r"\bhosting\b", r"\bcloud\b", r"\baws\b", r"\bazure\b"],
    "testing": [r"\btesting\b", r"\bpytest\b", r"\bunittest\b"],
    "status": [r"\bstatus\b", r"\bstage\b", r"\bphase\b", r"\bprogress\b"],
}


class MemoryGrounder:
    """
    Lightweight deterministic memory grounding layer.
    Identifies memory-dependent factual queries, evaluates whether retrieved memories
    contain the requested information, and prevents fabricating facts or project status.
    """

    @staticmethod
    def is_memory_fact_query(text: str) -> bool:
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        return any(re.search(pat, normalized) for pat in MEMORY_FACT_QUERY_PATTERNS)

    @staticmethod
    def is_suggestion_query(text: str) -> bool:
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        return any(re.search(pat, normalized) for pat in SUGGESTION_QUERY_PATTERNS)

    @staticmethod
    def detect_missing_aspect(text: str, retrieved_memories: Sequence[Memory]) -> str | None:
        """
        If the user asks for a specific technical aspect (e.g., database, framework)
        and no retrieved memory contains any keyword related to that aspect,
        return the aspect name so the assistant can accurately report uncertainty.
        """
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())
        combined_mem_text = " ".join(m.content.lower() for m in retrieved_memories)

        for aspect, patterns in SPECIFIC_ASPECT_PATTERNS.items():
            if any(re.search(pat, normalized) for pat in patterns):
                # User asked about this aspect. Check if memories mention it:
                if not any(re.search(pat, combined_mem_text) for pat in patterns):
                    return aspect

        return None

    @staticmethod
    def format_grounded_memory_context(
        memories: Sequence[Memory],
        is_fact_query: bool = False,
        is_suggestion: bool = False,
        missing_aspect: str | None = None,
    ) -> str:
        """
        Format memory context with strict anti-hallucination and grounding rules.
        """
        if not memories and not missing_aspect:
            return ""

        sections: list[str] = [
            "RELEVANT LONG-TERM MEMORY (RETRIEVED USER MEMORY — FACTUAL REFERENCE ONLY):",
            "The following are stored user-provided memories. Use them as the sole factual source of truth for questions asking about the user's personal facts, preferences, projects, or previously stored information.",
        ]

        if memories:
            for m in memories:
                clean_content = m.content.strip().lstrip("-•* ")
                sections.append(f"- {clean_content}")
        else:
            sections.append("(No relevant long-term memories were found in the database for this topic.)")

        sections.append("\nMEMORY GROUNDING & ANTI-HALLUCINATION RULES:")
        sections.append(
            "1. Do not invent or assume additional technologies, libraries, frameworks, or facts not explicitly written in the memories above."
        )
        sections.append(
            "2. Do not invent project status, stages, or phases (e.g. do NOT claim 'planning stage' or 'early stage') unless explicitly stated in the memory."
        )

        if missing_aspect:
            sections.append(
                f"3. UNCERTAINTY NOTICE: The user asked about '{missing_aspect}', but the stored memories do not mention any {missing_aspect}. You MUST explicitly state that you do not have stored memory confirming what {missing_aspect} is used. Do NOT guess or fabricate an answer."
            )
        elif is_fact_query:
            sections.append(
                "3. If a requested fact is not stated in the memories above, clearly state that you do not have a stored memory for it rather than guessing."
            )

        if is_suggestion:
            sections.append(
                "4. SUGGESTIONS ALLOWED: The user asked for suggestions/improvements. Clearly distinguish known project facts (under a 'What I know about your project:' header) from your recommendations (under a 'Suggestions:' header). Never present recommendations as already part of the project."
            )

        return "\n".join(sections)
