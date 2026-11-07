import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()


class ConfigurationError(ValueError):
    """Raised when environment or configuration parameters are invalid."""
    pass


def _validate_url(url_val: str, var_name: str) -> str:
    cleaned = url_val.strip()
    if not cleaned:
        raise ConfigurationError(f"Configuration value '{var_name}' cannot be empty.")
    try:
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"}:
            raise ConfigurationError(
                f"Invalid URL scheme '{parsed.scheme}' for '{var_name}'. Must be 'http' or 'https'."
            )
        if not parsed.netloc:
            raise ConfigurationError(
                f"Invalid URL for '{var_name}': missing hostname or network location."
            )
    except Exception as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(f"Malformed URL for '{var_name}': {exc}") from exc
    return cleaned


def _validate_float(
    raw_val: str | None,
    default: float,
    var_name: str,
    min_val: float = 0.001,
    max_val: float = 86400.0,
) -> float:
    if raw_val is None or str(raw_val).strip() == "":
        return default
    try:
        val = float(raw_val)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"Invalid numeric value '{raw_val}' for '{var_name}'. Expected float."
        ) from exc

    if val < min_val or val > max_val:
        raise ConfigurationError(
            f"Value {val} for '{var_name}' is out of bounds (allowed range: {min_val} to {max_val})."
        )
    return val


def _validate_int(
    raw_val: str | None,
    default: int,
    var_name: str,
    min_val: int = 1,
    max_val: int = 1_000_000_000,
) -> int:
    if raw_val is None or str(raw_val).strip() == "":
        return default
    try:
        val = int(raw_val)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"Invalid integer value '{raw_val}' for '{var_name}'. Expected integer."
        ) from exc

    if val < min_val or val > max_val:
        raise ConfigurationError(
            f"Value {val} for '{var_name}' is out of bounds (allowed range: {min_val} to {max_val})."
        )
    return val


def _validate_string(raw_val: str | None, default: str, var_name: str) -> str:
    if raw_val is None or str(raw_val).strip() == "":
        return default
    cleaned = str(raw_val).strip()
    # Reject shell metacharacters or suspicious control characters in model name
    if re.search(r"[\r\n\x00;`$|&<>]", cleaned):
        raise ConfigurationError(f"Illegal characters in configuration value '{var_name}'.")
    return cleaned


@dataclass(frozen=True)
class Settings:
    ollama_host: str
    ollama_model: str
    request_timeout: float
    max_history_messages: int
    # Resource limits for Phase 1 hardening
    max_document_size_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_extracted_chars_per_doc: int = 500_000        # 500k characters max
    max_extraction_units_per_doc: int = 5_000         # 5k slides/cells/units max
    approval_timeout_seconds: float = 300.0           # 5 minutes approval window


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """
    Safely load and validate settings from environment dictionary (or os.environ).
    Produces explicit ConfigurationError on invalid configurations.
    """
    get_env = env.get if env is not None else os.getenv

    host = _validate_url(get_env("OLLAMA_HOST", "http://127.0.0.1:11434"), "OLLAMA_HOST")
    model = _validate_string(get_env("OLLAMA_MODEL", "qwen2.5:3b"), "qwen2.5:3b", "OLLAMA_MODEL")
    timeout = _validate_float(get_env("OLLAMA_TIMEOUT", "120"), 120.0, "OLLAMA_TIMEOUT", min_val=1.0, max_val=3600.0)
    history = _validate_int(get_env("MAX_HISTORY_MESSAGES", "12"), 12, "MAX_HISTORY_MESSAGES", min_val=1, max_val=1000)

    doc_size = _validate_int(
        get_env("MAX_DOCUMENT_SIZE_BYTES", str(10 * 1024 * 1024)),
        10 * 1024 * 1024,
        "MAX_DOCUMENT_SIZE_BYTES",
        min_val=1024,
        max_val=1024 * 1024 * 1024,
    )
    max_chars = _validate_int(
        get_env("MAX_EXTRACTED_CHARS_PER_DOC", "500000"),
        500000,
        "MAX_EXTRACTED_CHARS_PER_DOC",
        min_val=100,
        max_val=50_000_000,
    )
    max_units = _validate_int(
        get_env("MAX_EXTRACTION_UNITS_PER_DOC", "5000"),
        5000,
        "MAX_EXTRACTION_UNITS_PER_DOC",
        min_val=1,
        max_val=100_000,
    )
    approval_timeout = _validate_float(
        get_env("APPROVAL_TIMEOUT_SECONDS", "300"),
        300.0,
        "APPROVAL_TIMEOUT_SECONDS",
        min_val=5.0,
        max_val=86400.0,
    )

    return Settings(
        ollama_host=host,
        ollama_model=model,
        request_timeout=timeout,
        max_history_messages=history,
        max_document_size_bytes=doc_size,
        max_extracted_chars_per_doc=max_chars,
        max_extraction_units_per_doc=max_units,
        approval_timeout_seconds=approval_timeout,
    )


settings = load_settings()
