import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    request_timeout: float = float(os.getenv("OLLAMA_TIMEOUT", "120"))
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))


settings = Settings()
