"""
Speech-to-Text (STT) Service for JARVIS Voice Subsystem (Milestone 5.1.2).
Provides local, privacy-preserving speech transcription via Whisper.cpp.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from typing import Optional

from voice.config import VoiceConfig, default_voice_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptionResult:
    """
    Structured result of a speech-to-text transcription attempt.

    Attributes:
        text: Transcribed text string (empty on failure or when no speech detected).
        success: True if transcription succeeded and valid speech text was extracted.
        error: Detailed error message if transcription failed, None otherwise.
        duration_seconds: Duration of the input audio in seconds.
        language: Detected or specified language code (e.g. 'en'), or None.
    """
    text: str
    success: bool
    error: Optional[str] = None
    duration_seconds: float = 0.0
    language: Optional[str] = None


class SpeechToTextError(Exception):
    """Base exception for speech-to-text errors."""
    pass


class SpeechToTextService:
    """
    Local Speech-to-Text service using Whisper.cpp.
    Operates strictly locally with zero external network or cloud dependencies.
    """

    def __init__(self, config: Optional[VoiceConfig] = None) -> None:
        """
        Initialize the speech-to-text service.

        Args:
            config: VoiceConfig instance. Defaults to default_voice_config.
        """
        self.config = config or default_voice_config

    def is_available(self) -> bool:
        """
        Check if the Whisper executable and model file are configured and exist.

        Returns:
            bool: True if both executable and model path exist on disk.
        """
        if not self.config.whisper_executable_path or not self.config.whisper_model_path:
            return False

        exe = self.config.whisper_executable_path.strip()
        if not (os.path.isfile(exe) or bool(shutil.which(exe))):
            return False

        model = self.config.whisper_model_path.strip()
        if not os.path.isfile(model):
            return False

        return True

    def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        """
        Transcribe raw PCM audio to text using local Whisper.cpp.

        Args:
            pcm_audio: Raw 16-bit 16kHz mono linear PCM audio bytes.

        Returns:
            TranscriptionResult with transcribed text and operational metadata.
        """
        # 1. Type validation
        if not isinstance(pcm_audio, (bytes, bytearray)):
            return TranscriptionResult(
                text="",
                success=False,
                error="Audio data must be bytes or bytearray",
                duration_seconds=0.0,
            )

        # 2. Empty audio validation
        if len(pcm_audio) == 0:
            return TranscriptionResult(
                text="",
                success=False,
                error="Audio data is empty",
                duration_seconds=0.0,
            )

        # 3. Audio format consistency check (Whisper requires 16kHz, mono, 16-bit PCM)
        if self.config.sample_rate != 16000 or self.config.channels != 1 or self.config.sample_width != 2:
            return TranscriptionResult(
                text="",
                success=False,
                error=(
                    f"Unsupported audio format for Whisper: {self.config.sample_rate}Hz, "
                    f"{self.config.channels}ch, {self.config.sample_width * 8}-bit. "
                    "Whisper requires 16000Hz, 1ch (mono), 16-bit PCM."
                ),
                duration_seconds=0.0,
            )

        # 4. PCM alignment validation
        if len(pcm_audio) % self.config.bytes_per_sample != 0:
            return TranscriptionResult(
                text="",
                success=False,
                error=(
                    f"Invalid PCM byte length ({len(pcm_audio)} bytes): not aligned with "
                    f"sample width of {self.config.bytes_per_sample} bytes."
                ),
                duration_seconds=0.0,
            )

        # Calculate audio duration
        duration_seconds = len(pcm_audio) / (self.config.sample_rate * self.config.bytes_per_sample)

        # 5. Maximum duration validation
        if len(pcm_audio) > self.config.max_audio_bytes:
            return TranscriptionResult(
                text="",
                success=False,
                error=(
                    f"Audio duration ({duration_seconds:.2f}s) exceeds maximum allowed duration "
                    f"({self.config.max_audio_duration_seconds:.2f}s)."
                ),
                duration_seconds=duration_seconds,
            )

        # 6. Availability and configuration validation
        if not self.config.whisper_executable_path:
            return TranscriptionResult(
                text="",
                success=False,
                error="Whisper executable path is not configured.",
                duration_seconds=duration_seconds,
            )

        exe_path = self.config.whisper_executable_path.strip()
        if not (os.path.isfile(exe_path) or bool(shutil.which(exe_path))):
            return TranscriptionResult(
                text="",
                success=False,
                error=f"Whisper executable not found at: {exe_path}",
                duration_seconds=duration_seconds,
            )

        if not self.config.whisper_model_path:
            return TranscriptionResult(
                text="",
                success=False,
                error="Whisper model path is not configured.",
                duration_seconds=duration_seconds,
            )

        model_path = self.config.whisper_model_path.strip()
        if not os.path.isfile(model_path):
            return TranscriptionResult(
                text="",
                success=False,
                error=f"Whisper model file not found at: {model_path}",
                duration_seconds=duration_seconds,
            )

        # 7. Safe temporary WAV file lifecycle
        temp_wav_path: Optional[str] = None

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
                temp_wav_path = temp_wav.name

            with wave.open(temp_wav_path, "wb") as wav_file:
                wav_file.setnchannels(self.config.channels)
                wav_file.setsampwidth(self.config.sample_width)
                wav_file.setframerate(self.config.sample_rate)
                wav_file.writeframes(pcm_audio)

            # Build command args safely without shell=True
            cmd = [
                exe_path,
                "-m", model_path,
                "-f", temp_wav_path,
                "-t", str(self.config.stt_threads),
                "-l", self.config.whisper_language,
                "-nt",
            ]
            if self.config.stt_output_format == "txt":
                cmd.append("-otxt")
            elif self.config.stt_output_format == "json":
                cmd.append("-oj")

            logger.debug(
                "Executing Whisper STT: format=%s, duration=%.2fs, language=%s, threads=%d",
                self.config.stt_output_format,
                duration_seconds,
                self.config.whisper_language,
                self.config.stt_threads,
            )

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.stt_timeout_seconds,
                    shell=False,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                logger.warning("Whisper STT process timed out after %.1fs", self.config.stt_timeout_seconds)
                return TranscriptionResult(
                    text="",
                    success=False,
                    error=f"Whisper transcription timed out after {self.config.stt_timeout_seconds:.1f}s.",
                    duration_seconds=duration_seconds,
                )
            except Exception as e:
                logger.error("Failed to execute Whisper subprocess: %s", type(e).__name__)
                return TranscriptionResult(
                    text="",
                    success=False,
                    error=f"Subprocess execution failed: {type(e).__name__}",
                    duration_seconds=duration_seconds,
                )

            # 8. Check process return code
            if proc.returncode != 0:
                sanitized_stderr = (proc.stderr or "").strip()
                if len(sanitized_stderr) > 500:
                    sanitized_stderr = sanitized_stderr[:500] + "... [truncated]"
                logger.warning("Whisper process exited with code %d: %s", proc.returncode, sanitized_stderr)
                return TranscriptionResult(
                    text="",
                    success=False,
                    error=f"Whisper process exited with non-zero code {proc.returncode}: {sanitized_stderr}",
                    duration_seconds=duration_seconds,
                )

            # 9. Detect language from stderr if configured as 'auto'
            detected_language = self._parse_language(proc.stderr, self.config.whisper_language)

            # 10. Extract transcribed text according to configured format
            raw_text = ""

            if self.config.stt_output_format == "txt":
                txt_output_path = temp_wav_path + ".txt"
                if not os.path.isfile(txt_output_path):
                    logger.warning("Whisper TXT output file not found at: %s", txt_output_path)
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error=f"Whisper TXT output file was not generated: {os.path.basename(txt_output_path)}",
                        duration_seconds=duration_seconds,
                    )
                try:
                    with open(txt_output_path, "r", encoding="utf-8", errors="replace") as f:
                        raw_text = f.read()
                except OSError as e:
                    logger.warning("Failed to read Whisper TXT output file: %s", e)
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error=f"Failed to read Whisper TXT output file: {e}",
                        duration_seconds=duration_seconds,
                    )

            elif self.config.stt_output_format == "json":
                json_output_path = temp_wav_path + ".json"
                if not os.path.isfile(json_output_path):
                    logger.warning("Whisper JSON output file not found at: %s", json_output_path)
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error=f"Whisper JSON output file was not generated: {os.path.basename(json_output_path)}",
                        duration_seconds=duration_seconds,
                    )
                try:
                    with open(json_output_path, "r", encoding="utf-8") as f:
                        json_content = f.read()
                except OSError as e:
                    logger.warning("Failed to read Whisper JSON output file: %s", e)
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error=f"Failed to read Whisper JSON output file: {e}",
                        duration_seconds=duration_seconds,
                    )

                if not json_content.strip():
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error="Whisper JSON output file is empty.",
                        duration_seconds=duration_seconds,
                    )

                try:
                    data = json.loads(json_content)
                except json.JSONDecodeError as e:
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error=f"Invalid JSON in Whisper output: {e.msg}",
                        duration_seconds=duration_seconds,
                    )

                if not isinstance(data, dict):
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error="Invalid JSON structure: top-level JSON must be an object.",
                        duration_seconds=duration_seconds,
                    )

                # Extract text from supported Whisper.cpp JSON schemas
                extracted_text: Optional[str] = None
                if "text" in data and isinstance(data["text"], str):
                    extracted_text = data["text"]
                elif "transcription" in data:
                    t_val = data["transcription"]
                    if isinstance(t_val, str):
                        extracted_text = t_val
                    elif isinstance(t_val, list):
                        segments = []
                        valid_segments = True
                        for item in t_val:
                            if isinstance(item, dict) and "text" in item and isinstance(item["text"], str):
                                seg_str = item["text"].strip()
                                if seg_str:
                                    segments.append(seg_str)
                            elif isinstance(item, str):
                                if item.strip():
                                    segments.append(item.strip())
                            else:
                                valid_segments = False
                                break

                        if not valid_segments:
                            return TranscriptionResult(
                                text="",
                                success=False,
                                error="Invalid JSON structure: transcription segments missing valid 'text' field.",
                                duration_seconds=duration_seconds,
                            )
                        extracted_text = " ".join(segments)

                if extracted_text is None:
                    return TranscriptionResult(
                        text="",
                        success=False,
                        error="Whisper JSON output missing expected transcription field ('text' or 'transcription').",
                        duration_seconds=duration_seconds,
                    )

                raw_text = extracted_text

                # Optionally supplement detected language from JSON metadata
                if detected_language is None:
                    if "result" in data and isinstance(data["result"], dict) and isinstance(data["result"].get("language"), str):
                        detected_language = data["result"]["language"].lower()
                    elif "language" in data and isinstance(data["language"], str):
                        detected_language = data["language"].lower()

            elif self.config.stt_output_format == "stdout":
                raw_text = proc.stdout or ""

            # 11. Clean and validate transcription
            cleaned_text = self._clean_transcription(raw_text)

            # Handle empty or blank transcriptions
            if not cleaned_text:
                return TranscriptionResult(
                    text="",
                    success=False,
                    error="No speech detected in audio.",
                    duration_seconds=duration_seconds,
                    language=detected_language,
                )

            # Reject unprintable / corrupted characters
            printable_ratio = sum(1 for ch in cleaned_text if ch.isprintable()) / len(cleaned_text)
            if printable_ratio < 0.8:
                return TranscriptionResult(
                    text="",
                    success=False,
                    error="Malformed transcription output: unexpected unprintable characters.",
                    duration_seconds=duration_seconds,
                    language=detected_language,
                )

            # Reject output containing no recognizable alphanumeric characters (pure punctuation, noise symbols)
            if not any(ch.isalnum() for ch in cleaned_text):
                return TranscriptionResult(
                    text="",
                    success=False,
                    error="Transcription output contains no recognizable speech content.",
                    duration_seconds=duration_seconds,
                    language=detected_language,
                )

            logger.info("Whisper STT transcribed successfully: duration=%.2fs, chars=%d", duration_seconds, len(cleaned_text))
            return TranscriptionResult(
                text=cleaned_text,
                success=True,
                duration_seconds=duration_seconds,
                language=detected_language,
            )

        except Exception as e:
            logger.error("Unexpected error during speech-to-text processing: %s", type(e).__name__)
            return TranscriptionResult(
                text="",
                success=False,
                error=f"Speech-to-text processing error: {type(e).__name__}",
                duration_seconds=duration_seconds,
            )

        finally:
            # 12. Clean up temporary files on success, failure, and cancellation
            if temp_wav_path:
                self._cleanup_temp_files(temp_wav_path)

    @staticmethod
    def _clean_transcription(raw_text: str) -> str:
        """
        Clean raw Whisper output: strip timestamps, blank audio markers, and whitespace.
        Preserves all valid multilingual spoken text.
        """
        if not raw_text:
            return ""

        # Remove timestamp markers like [00:00:00.000 --> 00:00:02.000]
        text = re.sub(r"\[\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}\]\s*", "", raw_text)

        # Remove known blank/silence markers emitted by whisper.cpp in brackets or parentheses
        text = re.sub(
            r"\[(BLANK_AUDIO|silence|music|applause|laughter|screaming|groan|sigh|cough|snort|inaudible|unintelligible|\*|\s*)\]",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\((BLANK_AUDIO|silence|music|applause|laughter|screaming|groan|sigh|cough|snort|inaudible|unintelligible|\*|\s*)\)",
            "",
            text,
            flags=re.IGNORECASE,
        )

        return text.strip()

    @staticmethod
    def _parse_language(stderr_text: Optional[str], configured_language: str) -> Optional[str]:
        """
        Extract detected language from stderr or return configured language.
        """
        if configured_language != "auto":
            return configured_language

        if not stderr_text:
            return None

        # Whisper.cpp auto-detection pattern:
        # e.g. "whisper_full_with_state: auto-detected language: en (p = 0.982143)"
        match = re.search(r"auto-detected language:\s*['\"]?([a-zA-Z_\-]+)['\"]?", stderr_text)
        if match:
            return match.group(1).lower()

        return None

    @staticmethod
    def _cleanup_temp_files(base_wav_path: str) -> None:
        """
        Safely remove temporary WAV and any Whisper-generated sidecar files.
        """
        for ext in ("", ".txt", ".json", ".csv", ".vtt", ".srt"):
            target = base_wav_path + ext if ext else base_wav_path
            if os.path.exists(target):
                try:
                    os.unlink(target)
                except OSError as e:
                    logger.warning("Failed to clean up temporary file %s: %s", target, e)
