from dataclasses import dataclass
from typing import Literal

ALLOWED_MEMORY_CATEGORIES = {
    "fact",
    "preference",
    "project",
    "task_context",
    "other",
}


@dataclass
class Memory:
    id: int
    content: str
    category: str
    importance: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.content or not self.content.strip():
            raise ValueError("Memory content cannot be empty.")
        if self.category not in ALLOWED_MEMORY_CATEGORIES:
            raise ValueError(
                f"Invalid memory category: '{self.category}'. "
                f"Allowed categories: {sorted(ALLOWED_MEMORY_CATEGORIES)}"
            )
        if not (1 <= self.importance <= 10):
            raise ValueError(
                f"Memory importance must be between 1 and 10, got {self.importance}."
            )


@dataclass
class ProfileEntry:
    key: str
    value: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.key or not self.key.strip():
            raise ValueError("Profile key cannot be empty.")


@dataclass
class Preference:
    id: int
    category: str
    key: str
    value: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.category or not self.category.strip():
            raise ValueError("Preference category cannot be empty.")
        if not self.key or not self.key.strip():
            raise ValueError("Preference key cannot be empty.")

