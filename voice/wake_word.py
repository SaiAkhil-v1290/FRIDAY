"""
Local Wake-Word Detection Subsystem for JARVIS (Milestone 5.1.4 & 5.1.4.1).
Continuously monitors raw PCM audio frames for a configured wake phrase (e.g. 'Hey Friday')
using lightweight local openWakeWord ONNX inference without cloud dependencies or Whisper invocations.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Union

import numpy as np

from voice.config import VoiceConfig, default_voice_config
from voice.audio_io import AudioCaptureService

logger = logging.getLogger(__name__)

# openWakeWord standard inference window parameters
OPENWAKEWORD_CHUNK_DURATION_MS: int = 80
OPENWAKEWORD_SAMPLE_RATE: int = 16000
OPENWAKEWORD_CHUNK_SAMPLES: int = 1280  # 80ms at 16kHz
OPENWAKEWORD_CHUNK_BYTES: int = 2560    # 1280 samples * 2 bytes/sample (16-bit PCM)


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
    Aggregates incoming 20ms capture frames into exact 80ms (1,280 samples) int16 chunks,
    performs local ONNX inference via openWakeWord or injected engine backends, enforces
    phrase/model identity mapping, applies detection cooldowns, and operates strictly offline.

    Audio format requirements:
        The openWakeWord backend requires 16 kHz mono signed 16-bit PCM audio (1,280 samples /
        80ms chunks). Sample rates other than 16,000 Hz are rejected during detector
        initialization when wake_word_engine == 'openwakeword'.
        If custom backends ('custom') are used with other audio configurations, the custom
        backend is responsible for resampling, frame aggregation, and format adaptation.
    """

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        engine: Optional[Union[Any, Callable[[np.ndarray], Union[float, dict[str, float]]]]] = None,
    ) -> None:
        """
        Initialize the WakeWordDetector with configuration and optional engine backend.

        Args:
            config: Shared VoiceConfig. Defaults to default_voice_config.
            engine: Optional injectable engine backend or callable for testing / custom models.

        Raises:
            ValueError: If sample_rate != 16000 and wake_word_engine == 'openwakeword'.
        """
        self.config = config or default_voice_config

        if self.config.wake_word_engine == "openwakeword" and self.config.sample_rate != 16000:
            raise ValueError(
                f"openWakeWord backend requires sample_rate=16000 Hz, got sample_rate={self.config.sample_rate}. "
                "openWakeWord operates exclusively on 16 kHz mono signed 16-bit PCM audio. "
                "Resampling is not supported in this milestone."
            )

        self._lock = threading.RLock()
        self._audio_buffer = bytearray()
        self._last_detection_timestamp: float = 0.0
        self._is_stopped: bool = False
        self._engine = engine
        self._initialization_error: Optional[str] = None

        if self._engine is None and self.config.wake_word_engine == "openwakeword":
            self._init_openwakeword_engine()

    def _init_openwakeword_engine(self) -> None:
        """Attempt to initialize the openWakeWord ONNX engine backend once."""
        model_path = self.config.wake_word_model_path
        if not model_path:
            self._initialization_error = "No wake_word_model_path configured."
            logger.debug(self._initialization_error)
            return

        clean_path = model_path.strip()
        if not os.path.isfile(clean_path):
            self._initialization_error = f"Configured wake_word_model_path not found on disk: {clean_path}"
            logger.debug(self._initialization_error)
            return

        try:
            import openwakeword
            from openwakeword.model import Model

            logger.info("Initializing openWakeWord ONNX model from: %s", clean_path)
            model_instance = Model(
                wakeword_models=[clean_path],
                inference_framework=self.config.wake_word_inference_framework,
            )

            # Validate that the model loaded successfully and verify model keys
            if hasattr(model_instance, "models") and isinstance(model_instance.models, dict):
                loaded_keys = list(model_instance.models.keys())
                if not loaded_keys:
                    self._initialization_error = f"openWakeWord model loaded but contains no models: {clean_path}"
                    logger.error(self._initialization_error)
                    return

                if not any(self._key_matches_phrase(k) for k in loaded_keys):
                    logger.warning(
                        "Loaded openWakeWord model keys %s do not match configured phrase '%s'",
                        loaded_keys,
                        self.config.wake_word_phrase,
                    )

            self._engine = model_instance
            self._initialization_error = None
        except ImportError as e:
            self._initialization_error = f"openwakeword package is not installed: {e}"
            logger.debug(self._initialization_error)
            self._engine = None
        except Exception as e:
            self._initialization_error = f"Failed to initialize openWakeWord ONNX engine: {type(e).__name__}: {e}"
            logger.error(self._initialization_error)
            self._engine = None

    def is_available(self) -> bool:
        """
        Check if the wake-word engine backend and model are initialized and ready to process audio.

        Returns:
            bool: True only if the engine was successfully loaded and initialized.
        """
        with self._lock:
            if self._is_stopped:
                return False
            return self._engine is not None

    @property
    def initialization_error(self) -> Optional[str]:
        """Return the initialization failure reason if unavailable, or None."""
        with self._lock:
            return self._initialization_error

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

    def _key_matches_phrase(self, key: str) -> bool:
        """
        Check if a model output key matches the configured phrase or explicit model key.

        Args:
            key: Output key returned by the detection engine.

        Returns:
            bool: True if key corresponds to the configured wake phrase.
        """
        if self.config.wake_word_model_key:
            return key.strip().lower() == self.config.wake_word_model_key.strip().lower()

        clean_key = str(key).strip().lower().replace("_", " ").replace("-", " ")
        clean_phrase = self.config.wake_word_phrase.strip().lower().replace("_", " ").replace("-", " ")

        if clean_key == clean_phrase:
            return True
        if key.strip().lower() == clean_phrase.replace(" ", "_"):
            return True
        if clean_key.startswith(clean_phrase):
            return True
        return False

    def process_frame(self, frame: bytes) -> WakeWordResult:
        """
        Process a single incoming raw PCM audio frame (e.g. 20ms / 640 bytes).
        Accumulates frames until an 80ms chunk (1,280 samples / 2,560 bytes) is reached,
        converts the chunk to a signed int16 NumPy array, and runs inference.

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
                    error=self._initialization_error or "Wake-word engine or model file is unavailable.",
                )

            # 6. Append frame to internal audio buffer
            self._audio_buffer.extend(frame)

            # 7. Check if a complete 80ms chunk (2560 bytes = 1280 samples) is available
            if len(self._audio_buffer) < OPENWAKEWORD_CHUNK_BYTES:
                # Incomplete chunk: no inference call is performed
                return WakeWordResult(
                    detected=False,
                    wake_phrase=self.config.wake_word_phrase,
                    confidence=0.0,
                    timestamp=now,
                )

            # 8. Process complete 80ms chunk(s)
            last_confidence = 0.0
            detected = False
            detected_error = None

            while len(self._audio_buffer) >= OPENWAKEWORD_CHUNK_BYTES:
                # Extract exact 80ms chunk and advance buffer (retaining remainder)
                chunk_bytes = bytes(self._audio_buffer[:OPENWAKEWORD_CHUNK_BYTES])
                del self._audio_buffer[:OPENWAKEWORD_CHUNK_BYTES]

                # Check cooldown: suppress detection if within cooldown window
                if self._last_detection_timestamp > 0.0:
                    elapsed_since_last = now - self._last_detection_timestamp
                    if elapsed_since_last < self.config.wake_word_cooldown_seconds:
                        logger.debug(
                            "Wake-word cooldown active (%.2fs < %.2fs); suppressing detection.",
                            elapsed_since_last,
                            self.config.wake_word_cooldown_seconds,
                        )
                        continue

                # Convert raw PCM bytes to signed 16-bit NumPy array
                try:
                    audio_array = np.frombuffer(chunk_bytes, dtype=np.int16)
                except Exception as conv_err:
                    return WakeWordResult(
                        detected=False,
                        wake_phrase=self.config.wake_word_phrase,
                        timestamp=now,
                        error=f"PCM to NumPy int16 conversion failed: {conv_err}",
                    )

                # Invoke detection engine
                try:
                    raw_pred = self._invoke_engine(audio_array)
                except Exception as eng_err:
                    logger.error("Wake-word engine exception: %s", eng_err)
                    return WakeWordResult(
                        detected=False,
                        wake_phrase=self.config.wake_word_phrase,
                        confidence=0.0,
                        timestamp=now,
                        error=f"Detection engine error: {type(eng_err).__name__}: {eng_err}",
                    )

                conf, err = self._extract_confidence(raw_pred)
                if err is not None:
                    detected_error = err
                    last_confidence = 0.0
                    continue

                last_confidence = conf

                # Threshold comparison
                if conf >= self.config.wake_word_threshold:
                    detected = True
                    self._last_detection_timestamp = now
                    # Clear buffer on activation to avoid repeated detections from the same utterance
                    self._audio_buffer.clear()
                    logger.info(
                        "Wake-word '%s' detected with confidence %.3f (threshold=%.2f)",
                        self.config.wake_word_phrase,
                        conf,
                        self.config.wake_word_threshold,
                    )
                    break

            # 9. Enforce buffer memory bound on incomplete remainder
            max_buffer_bytes = int(
                self.config.sample_rate
                * self.config.bytes_per_sample
                * self.config.wake_word_buffer_duration_sec
            )
            if len(self._audio_buffer) > max_buffer_bytes:
                del self._audio_buffer[:-max_buffer_bytes]

            if detected:
                return WakeWordResult(
                    detected=True,
                    wake_phrase=self.config.wake_word_phrase,
                    confidence=last_confidence,
                    timestamp=now,
                )

            return WakeWordResult(
                detected=False,
                wake_phrase=self.config.wake_word_phrase,
                confidence=last_confidence,
                timestamp=now,
                error=detected_error,
            )

    def _invoke_engine(self, audio_array: np.ndarray) -> Any:
        """
        Invoke the engine backend with signed int16 NumPy array.
        Directly passes the int16 NumPy array to predict() or the callable engine.
        Does not catch or hide TypeErrors with raw bytes retries.

        Args:
            audio_array: NumPy ndarray of dtype int16 containing 1,280 samples (80ms at 16 kHz).

        Returns:
            Any: Engine prediction output (dict of scores or scalar confidence).

        Raises:
            WakeWordEngineError: If engine backend has no predict method and is not callable.
        """
        if self._engine is None:
            return 0.0

        if hasattr(self._engine, "predict"):
            return self._engine.predict(audio_array)
        elif callable(self._engine):
            return self._engine(audio_array)
        else:
            raise WakeWordEngineError(
                f"Engine {type(self._engine)} has no predict method and is not callable."
            )

    def _extract_confidence(self, result: Any) -> tuple[float, Optional[str]]:
        """
        Extract confidence score matching the configured wake phrase from engine prediction.
        Enforces strict model key identity to prevent unrelated models from triggering detection.

        Args:
            result: Raw output from the engine (dict or scalar).

        Returns:
            tuple[float, Optional[str]]: (confidence, error_message if mismatch).
        """
        if result is None:
            return 0.0, None

        if isinstance(result, (int, float)):
            return max(0.0, min(1.0, float(result))), None

        if isinstance(result, dict):
            if not result:
                return 0.0, None

            matching_val = None
            for key, val in result.items():
                if self._key_matches_phrase(key):
                    matching_val = val
                    break

            if matching_val is None:
                # Model output does not contain the configured wake phrase!
                avail_keys = list(result.keys())
                error_msg = (
                    f"Model output mismatch: engine output keys {avail_keys} do not match "
                    f"configured wake phrase '{self.config.wake_word_phrase}'."
                )
                logger.warning(error_msg)
                return 0.0, error_msg

            try:
                return max(0.0, min(1.0, float(matching_val))), None
            except (ValueError, TypeError):
                return 0.0, f"Non-numeric score for matching key: {matching_val}"

        return 0.0, f"Unsupported engine prediction format: {type(result).__name__}"

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
