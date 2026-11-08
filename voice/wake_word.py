"""
Local Wake-Word Detection Subsystem for JARVIS (Milestone 5.1.4).
Continuously monitors raw PCM audio frames for a configured wake phrase (e.g. 'Hey Friday')
using lightweight local inference without cloud dependencies or full ASR/Whisper invocations.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Union

from voice.config import VoiceConfig, default_voice_config
from voice.audio_io import AudioCaptureService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeWordResult:
    """
    Structured outcome of wake-word analysis on audio frame(s).

    Attributes:
        detected: True if the configured wake phrase was identified.
        wake_phrase: The phrase monitored (e.g. 'Hey Friday').
        confidence: Detection confidence score between 0.0 and 1.0.
        timestamp: Unix timestamp (from time.time()) when result was produced.
        error: Descriptive error message if detection encountered an issue.
    """
    detected: bool
    wake_phrase: str
    confidence: float = 0.0
    timestamp: float = 0.0
    error: Optional[str] = None


class WakeWordError(Exception):
    """Base exception for wake-word subsystem."""
    pass


class WakeWordModelError(WakeWordError):
    """Raised when a wake-word model is missing, corrupt, or invalid."""
    pass


class WakeWordEngineError(WakeWordError):
    """Raised when the wake-word engine backend fails or cannot be loaded."""
    pass


class WakeWordDetector:
    """
    Local Wake-Word Detection service.
    Processes continuous PCM audio frames from AudioCaptureService, detects the configured
    wake phrase using local inference (openWakeWord / custom backends), enforces detection cooldowns,
    prevents duplicate activations, and operates strictly offline with zero cloud dependencies.
    """

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        engine: Optional[Union[Any, Callable[[bytes], Union[float, dict[str, float]]]]] = None,
    ) -> None:
        """
        Initialize the WakeWordDetector with configuration and optional engine backend.

        Args:
            config: Shared VoiceConfig. Defaults to default_voice_config.
            engine: Optional injectable engine backend or callable for testing / custom models.
        """
        self.config = config or default_voice_config
        self._lock = threading.RLock()
        self._audio_buffer = bytearray()
        self._last_detection_timestamp: float = 0.0
        self._is_stopped: bool = False
        self._engine = engine

        if self._engine is None and self.config.wake_word_engine == "openwakeword":
            self._init_openwakeword_engine()

    def _init_openwakeword_engine(self) -> None:
        """Attempt to initialize the openWakeWord engine backend."""
        model_path = self.config.wake_word_model_path
        if not model_path:
            logger.debug("No wake_word_model_path configured; openWakeWord engine inactive.")
            return

        clean_path = model_path.strip()
        if not os.path.isfile(clean_path):
            logger.debug("Configured wake_word_model_path does not exist on disk: %s", clean_path)
            return

        try:
            import openwakeword
            from openwakeword.model import Model

            logger.info("Initializing openWakeWord model from: %s", clean_path)
            self._engine = Model(wakeword_models=[clean_path], inference_framework="onnx")
        except ImportError:
            logger.debug("openwakeword package is not installed.")
            self._engine = None
        except Exception as e:
            logger.error("Failed to initialize openWakeWord engine: %s", e)
            self._engine = None

    def is_available(self) -> bool:
        """
        Check if the wake-word engine backend and model are available locally.

        Returns:
            bool: True if engine is loaded and ready to process audio.
        """
        with self._lock:
            if self._is_stopped:
                return False
            if self._engine is not None:
                return True

            # If not yet initialized, check if openWakeWord and model file exist
            if self.config.wake_word_engine == "openwakeword":
                model_path = self.config.wake_word_model_path
                if not model_path or not os.path.isfile(model_path.strip()):
                    return False
                try:
                    import openwakeword  # noqa: F401
                    return True
                except ImportError:
                    return False

            return False

    @property
    def is_active(self) -> bool:
        """Check whether the detector is active (not stopped)."""
        with self._lock:
            return not self._is_stopped

    @property
    def wake_phrase(self) -> str:
        """Return the currently configured wake phrase."""
        return self.config.wake_word_phrase

    @property
    def threshold(self) -> float:
        """Return the detection confidence threshold."""
        return self.config.wake_word_threshold

    @property
    def cooldown_seconds(self) -> float:
        """Return the detection cooldown duration in seconds."""
        return self.config.wake_word_cooldown_seconds

    @property
    def last_detection_timestamp(self) -> float:
        """Return the Unix timestamp of the most recent detection."""
        with self._lock:
            return self._last_detection_timestamp

    def process_frame(self, frame: bytes) -> WakeWordResult:
        """
        Process a single incoming raw PCM audio frame.

        Args:
            frame: Linear PCM 16-bit mono audio bytes matching VoiceConfig.

        Returns:
            WakeWordResult with detection status, confidence, and metadata.
        """
        with self._lock:
            now = time.time()

            # 1. Stopped state check
            if self._is_stopped:
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    timestamp=now,
                    error="WakeWordDetector has been stopped.",
                )

            # 2. Audio type validation
            if not isinstance(frame, (bytes, bytearray)):
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    timestamp=now,
                    error=f"Audio frame must be bytes or bytearray, got {type(frame).__name__}.",
                )

            # 3. Audio length validation
            if len(frame) == 0:
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    timestamp=now,
                    error="Audio frame is empty.",
                )

            # 4. PCM alignment validation
            if len(frame) % self.config.bytes_per_sample != 0:
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    timestamp=now,
                    error=(
                        f"Audio frame length ({len(frame)}) is not aligned to "
                        f"sample width ({self.config.bytes_per_sample} bytes)."
                    ),
                )

            # 5. Availability check
            if not self.is_available():
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    timestamp=now,
                    error="Wake-word engine or model file is unavailable.",
                )

            # 6. Cooldown & duplicate activation prevention
            if self._last_detection_timestamp > 0.0:
                elapsed_since_last = now - self._last_detection_timestamp
                if elapsed_since_last < self.config.wake_word_cooldown_seconds:
                    logger.debug(
                        "Wake-word cooldown active (%.2fs < %.2fs); suppressing detection.",
                        elapsed_since_last,
                        self.config.wake_word_cooldown_seconds,
                    )
                    return WakeWordResult(
                        detected=False,
                        wake_phrase=self.config.wake_word_phrase,
                        confidence=0.0,
                        timestamp=now,
                    )

            # 7. Internal buffer accumulation and bounding
            self._audio_buffer.extend(frame)
            max_buffer_bytes = int(
                self.config.sample_rate
                * self.config.bytes_per_sample
                * self.config.wake_word_buffer_duration_sec
            )
            if len(self._audio_buffer) > max_buffer_bytes:
                # Retain the most recent window bounded by max_buffer_bytes
                del self._audio_buffer[:-max_buffer_bytes]

            # 8. Engine prediction invocation
            confidence = 0.0
            try:
                confidence = self._invoke_engine(bytes(self._audio_buffer))
            except Exception as e:
                logger.error("Wake-word engine exception: %s", e)
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    confidence=0.0,
                    timestamp=now,
                    error=f"Detection engine error: {type(e).__name__}: {e}",
                )

            # 9. Threshold comparison & detection transition
            if confidence >= self.config.wake_word_threshold:
                self._last_detection_timestamp = now
                # Clear audio buffer on activation so the same utterance does not immediately re-trigger
                self._audio_buffer.clear()
                logger.info(
                    "Wake-word '%s' detected with confidence %.3f (threshold=%.2f)",
                    self.config.wake_word_phrase,
                    confidence,
                    self.config.wake_word_threshold,
                )
                return WakeWordResult(
                    detected=True,
                    wake_phrase=self.config.wake_word_phrase,
                    confidence=confidence,
                    timestamp=now,
                )

            return WakeWordResult(
                detected=False,
                wake_phrase=self.config.wake_word_phrase,
                confidence=confidence,
                timestamp=now,
            )

    def _invoke_engine(self, pcm_data: bytes) -> float:
        """
        Invoke the underlying detection engine on PCM audio bytes.

        Args:
            pcm_data: Linear PCM audio bytes.

        Returns:
            float: Confidence score in [0.0, 1.0].
        """
        if self._engine is None:
            return 0.0

        # Injectable engine callable or object with predict()
        raw_result: Any = None
        if hasattr(self._engine, "predict"):
            raw_result = self._engine.predict(pcm_data)
        elif callable(self._engine):
            raw_result = self._engine(pcm_data)
        else:
            raise WakeWordEngineError(f"Engine object {type(self._engine)} has no predict method and is not callable.")

        return self._extract_confidence(raw_result)

    def _extract_confidence(self, result: Any) -> float:
        """Extract a normalized float confidence score from engine prediction result."""
        if result is None:
            return 0.0

        if isinstance(result, (int, float)):
            return max(0.0, min(1.0, float(result)))

        if isinstance(result, dict):
            if not result:
                return 0.0

            # 1. Exact phrase match
            norm_phrase = self.config.wake_word_phrase.strip().lower()
            key_candidates = [
                norm_phrase,
                norm_phrase.replace(" ", "_"),
                norm_phrase.replace(" ", ""),
            ]
            for key in key_candidates:
                if key in result:
                    return max(0.0, min(1.0, float(result[key])))

            # 2. Case-insensitive key match
            for k, val in result.items():
                k_clean = str(k).strip().lower().replace("_", " ")
                if k_clean == norm_phrase:
                    return max(0.0, min(1.0, float(val)))

            # 3. Fallback: take maximum score from dictionary values
            try:
                numeric_vals = [float(v) for v in result.values() if isinstance(v, (int, float))]
                return max(0.0, min(1.0, max(numeric_vals))) if numeric_vals else 0.0
            except (ValueError, TypeError):
                return 0.0

        return 0.0

    def listen_for_wake_word(
        self,
        capture_service: AudioCaptureService,
        timeout: Optional[float] = None,
    ) -> Optional[WakeWordResult]:
        """
        Continuously read frames from an active AudioCaptureService until wake-word is detected or timeout.

        Args:
            capture_service: Running AudioCaptureService instance.
            timeout: Maximum seconds to listen. None for indefinite until detection.

        Returns:
            WakeWordResult if detected, None if timeout expired without detection.
        """
        deadline = (time.time() + timeout) if timeout is not None else float("inf")
        while time.time() < deadline:
            with self._lock:
                if self._is_stopped:
                    break

            frame = capture_service.read_frame(timeout=0.05)
            if frame is None:
                continue

            result = self.process_frame(frame)
            if result.detected:
                return result

        return None

    def reset(self) -> None:
        """Reset internal audio buffers and cooldown timestamp."""
        with self._lock:
            self._audio_buffer.clear()
            self._last_detection_timestamp = 0.0
            if hasattr(self._engine, "reset") and callable(self._engine.reset):
                try:
                    self._engine.reset()
                except Exception as e:
                    logger.warning("Error resetting wake-word engine: %s", e)

    def stop(self) -> None:
        """Shutdown detector and release engine resources."""
        with self._lock:
            if self._is_stopped:
                return
            self._is_stopped = True
            self._audio_buffer.clear()
            if hasattr(self._engine, "stop") and callable(self._engine.stop):
                try:
                    self._engine.stop()
                except Exception as e:
                    logger.warning("Error stopping wake-word engine: %s", e)
            elif hasattr(self._engine, "close") and callable(self._engine.close):
                try:
                    self._engine.close()
                except Exception as e:
                    logger.warning("Error closing wake-word engine: %s", e)
