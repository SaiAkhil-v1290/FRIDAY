from datetime import datetime, timezone
from pathlib import Path
from memory.database import DatabaseManager
from memory.models import (
    Memory,
    ProfileEntry,
    Preference,
    ALLOWED_MEMORY_CATEGORIES,
)


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class LongTermMemory:
    """
    Persistent SQLite long-term memory engine for facts, profile, and preferences.
    Uses parameterized queries and Python validation for all operations.
    """

    def __init__(self, db: DatabaseManager | None = None, db_path: str | Path | None = None) -> None:
        if db is not None:
            self.db = db
        else:
            self.db = DatabaseManager(db_path=db_path)

    # -------------------------------------------------------------------------
    # GENERAL MEMORIES API
    # -------------------------------------------------------------------------

    def add_memory(
        self,
        content: str,
        category: str = "other",
        importance: int = 5,
    ) -> Memory:
        cleaned_content = content.strip() if content else ""
        if not cleaned_content:
            raise ValueError("Memory content cannot be empty.")
        if category not in ALLOWED_MEMORY_CATEGORIES:
            raise ValueError(
                f"Invalid memory category: '{category}'. "
                f"Allowed categories: {sorted(ALLOWED_MEMORY_CATEGORIES)}"
            )
        if not (1 <= importance <= 10):
            raise ValueError(
                f"Memory importance must be between 1 and 10, got {importance}."
            )

        with self.db.connection() as conn:
            cursor = conn.cursor()
            # Prevent exact duplicate memories
            cursor.execute(
                "SELECT id, content, category, importance, created_at, updated_at "
                "FROM memories WHERE content = ? AND category = ?",
                (cleaned_content, category),
            )
            existing = cursor.fetchone()
            if existing:
                return Memory(
                    id=existing["id"],
                    content=existing["content"],
                    category=existing["category"],
                    importance=existing["importance"],
                    created_at=existing["created_at"],
                    updated_at=existing["updated_at"],
                )

            now = _current_timestamp()
            cursor.execute(
                "INSERT INTO memories (content, category, importance, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cleaned_content, category, importance, now, now),
            )
            memory_id = cursor.lastrowid
            return Memory(
                id=memory_id,
                content=cleaned_content,
                category=category,
                importance=importance,
                created_at=now,
                updated_at=now,
            )

    def get_memory(self, memory_id: int) -> Memory | None:
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, category, importance, created_at, updated_at "
                "FROM memories WHERE id = ?",
                (memory_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Memory(
                id=row["id"],
                content=row["content"],
                category=row["category"],
                importance=row["importance"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_memories(
        self,
        category: str | None = None,
        limit: int = 100,
    ) -> list[Memory]:
        safe_limit = max(1, min(limit, 500))
        with self.db.connection() as conn:
            cursor = conn.cursor()
            if category:
                cursor.execute(
                    "SELECT id, content, category, importance, created_at, updated_at "
                    "FROM memories WHERE category = ? ORDER BY id DESC LIMIT ?",
                    (category, safe_limit),
                )
            else:
                cursor.execute(
                    "SELECT id, content, category, importance, created_at, updated_at "
                    "FROM memories ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                )
            rows = cursor.fetchall()
            return [
                Memory(
                    id=row["id"],
                    content=row["content"],
                    category=row["category"],
                    importance=row["importance"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def search_memories(
        self,
        query: str,
        limit: int = 10,
    ) -> list[Memory]:
        cleaned_query = query.strip()
        if not cleaned_query:
            return []

        safe_limit = max(1, min(limit, 50))
        with self.db.connection() as conn:
            cursor = conn.cursor()
            # Case-insensitive LIKE matching using parameterized query
            param = f"%{cleaned_query}%"
            cursor.execute(
                "SELECT id, content, category, importance, created_at, updated_at "
                "FROM memories WHERE content LIKE ? ORDER BY importance DESC, id DESC LIMIT ?",
                (param, safe_limit),
            )
            rows = cursor.fetchall()
            return [
                Memory(
                    id=row["id"],
                    content=row["content"],
                    category=row["category"],
                    importance=row["importance"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def update_memory(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
    ) -> Memory | None:
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, category, importance, created_at, updated_at "
                "FROM memories WHERE id = ?",
                (memory_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            new_content = row["content"]
            if content is not None:
                cleaned_content = content.strip()
                if not cleaned_content:
                    raise ValueError("Memory content cannot be empty.")
                new_content = cleaned_content

            new_category = row["category"]
            if category is not None:
                if category not in ALLOWED_MEMORY_CATEGORIES:
                    raise ValueError(
                        f"Invalid memory category: '{category}'. "
                        f"Allowed categories: {sorted(ALLOWED_MEMORY_CATEGORIES)}"
                    )
                new_category = category

            new_importance = row["importance"]
            if importance is not None:
                if not (1 <= importance <= 10):
                    raise ValueError(
                        f"Memory importance must be between 1 and 10, got {importance}."
                    )
                new_importance = importance

            now = _current_timestamp()
            cursor.execute(
                "UPDATE memories SET content = ?, category = ?, importance = ?, updated_at = ? "
                "WHERE id = ?",
                (new_content, new_category, new_importance, now, memory_id),
            )
            return Memory(
                id=memory_id,
                content=new_content,
                category=new_category,
                importance=new_importance,
                created_at=row["created_at"],
                updated_at=now,
            )

    def delete_memory(self, memory_id: int) -> bool:
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # PROFILE API
    # -------------------------------------------------------------------------

    def set_profile(self, key: str, value: str) -> ProfileEntry:
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if not cleaned_key:
            raise ValueError("Profile key cannot be empty.")

        now = _current_timestamp()
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO profile (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (cleaned_key, cleaned_value, now),
            )
            return ProfileEntry(key=cleaned_key, value=cleaned_value, updated_at=now)

    def get_profile(self, key: str) -> ProfileEntry | None:
        cleaned_key = key.strip()
        if not cleaned_key:
            return None

        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value, updated_at FROM profile WHERE key = ?",
                (cleaned_key,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ProfileEntry(
                key=row["key"],
                value=row["value"],
                updated_at=row["updated_at"],
            )

    def list_profile(self) -> list[ProfileEntry]:
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value, updated_at FROM profile ORDER BY key ASC")
            rows = cursor.fetchall()
            return [
                ProfileEntry(
                    key=row["key"],
                    value=row["value"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def delete_profile(self, key: str) -> bool:
        cleaned_key = key.strip()
        if not cleaned_key:
            return False

        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM profile WHERE key = ?", (cleaned_key,))
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # PREFERENCES API
    # -------------------------------------------------------------------------

    def set_preference(self, category: str, key: str, value: str) -> Preference:
        cleaned_category = category.strip()
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if not cleaned_category:
            raise ValueError("Preference category cannot be empty.")
        if not cleaned_key:
            raise ValueError("Preference key cannot be empty.")

        now = _current_timestamp()
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO preferences (category, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(category, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (cleaned_category, cleaned_key, cleaned_value, now),
            )
            # Retrieve the id
            cursor.execute(
                "SELECT id, category, key, value, updated_at FROM preferences "
                "WHERE category = ? AND key = ?",
                (cleaned_category, cleaned_key),
            )
            row = cursor.fetchone()
            return Preference(
                id=row["id"],
                category=row["category"],
                key=row["key"],
                value=row["value"],
                updated_at=row["updated_at"],
            )

    def get_preference(self, category: str, key: str) -> Preference | None:
        cleaned_category = category.strip()
        cleaned_key = key.strip()
        if not cleaned_category or not cleaned_key:
            return None

        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, category, key, value, updated_at FROM preferences "
                "WHERE category = ? AND key = ?",
                (cleaned_category, cleaned_key),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Preference(
                id=row["id"],
                category=row["category"],
                key=row["key"],
                value=row["value"],
                updated_at=row["updated_at"],
            )

    def list_preferences(self, category: str | None = None) -> list[Preference]:
        with self.db.connection() as conn:
            cursor = conn.cursor()
            if category:
                cursor.execute(
                    "SELECT id, category, key, value, updated_at FROM preferences "
                    "WHERE category = ? ORDER BY key ASC",
                    (category.strip(),),
                )
            else:
                cursor.execute(
                    "SELECT id, category, key, value, updated_at FROM preferences "
                    "ORDER BY category ASC, key ASC"
                )
            rows = cursor.fetchall()
            return [
                Preference(
                    id=row["id"],
                    category=row["category"],
                    key=row["key"],
                    value=row["value"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def delete_preference(self, category: str, key: str) -> bool:
        cleaned_category = category.strip()
        cleaned_key = key.strip()
        if not cleaned_category or not cleaned_key:
            return False

        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM preferences WHERE category = ? AND key = ?",
                (cleaned_category, cleaned_key),
            )
            return cursor.rowcount > 0

