"""
Voice Activity Detection (VAD) Subsystem for JARVIS (Milestone 5.1.1).
Provides frame validation, speech activity detection, speech-start/speech-end state tracking,
configurable silence duration thresholds, and bounded in-memory utterance accumulation.
"""

from dataclasses import dataclass
from typing import Literal
import math
import struct

from voice.config import VoiceConfig, default_voice_config

VADEventType = Literal["speech_start", "speech_end", "speech_frame", "silence_frame", "buffer_overflow"]


class InvalidFrameError(ValueError):
    """Raised when an audio frame is empty, corrupted, or does not match expected byte length."""
    pass


@dataclass(frozen=True)
class VADEvent:
    """Represents a discrete voice activity transition or classified frame."""
    event_type: VADEventType
    timestamp_ms: float
    frame: bytes
    duration_ms: float
    accumulated_audio: bytes | None = None
    truncated: bool = False
    pre_speech_truncated: bool = False


class VoiceActivityDetector:
    """
    Lightweight, robust Voice Activity Detector.
    Uses Google WebRTC VAD as the primary detection engine, with deterministic fallback
    to energy/RMS analysis if WebRTC VAD is unavailable or custom frame sizes are used.
    Maintains clean state transitions for speech start and speech end events.
    """

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or default_voice_config
        self._vad = None
        self._init_webrtc_vad()

        self._is_speech_active = False
        self._is_truncated = False
        self._is_pre_speech_truncated = False
        self._speech_accumulated_ms = 0.0
        self._silence_accumulated_ms = 0.0
        self._speech_buffer = bytearray()
        self._pre_speech_buffer = bytearray()
        self._total_processed_frames = 0

    def _init_webrtc_vad(self) -> None:
        """Attempt to initialize webrtcvad backend with configured aggressiveness."""
        try:
            import webrtcvad
            self._vad = webrtcvad.Vad(self.config.vad_mode)
        except (ImportError, Exception):
            self._vad = None

    @property
    def is_speech_active(self) -> bool:
        """Return True if currently inside an active spoken utterance."""
        return self._is_speech_active

    @property
    def is_truncated(self) -> bool:
        """Return True if either pre-speech or utterance audio was truncated due to buffer limits."""
        return self._is_truncated or self._is_pre_speech_truncated

    @property
    def is_pre_speech_truncated(self) -> bool:
        """Return True if pre-speech onset audio for the current utterance was truncated."""
        return self._is_pre_speech_truncated

    @property
    def buffered_duration_sec(self) -> float:
        """Return duration in seconds of currently accumulated utterance audio."""
        return len(self._speech_buffer) / float(self.config.sample_rate * self.config.bytes_per_sample)

    @property
    def pre_speech_buffered_bytes(self) -> int:
        """Return byte count of currently held pre-speech frames."""
        return len(self._pre_speech_buffer)

    def is_speech(self, frame: bytes) -> bool:
        """
        Evaluate whether an individual PCM frame contains speech activity.
        Uses WebRTC VAD if available; otherwise calculates root-mean-square (RMS) energy.
        Applies baseline silence floor check to prevent GMM hang-over on pure silence frames.
        """
        self._validate_frame(frame)

        # Baseline silence floor: if frame energy is physically below ambient noise floor,
        # it is classified as silence immediately without statistical hang-over.
        if self._is_silence_energy(frame):
            return False

        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, self.config.sample_rate)
            except Exception:
                # Fall back to energy calculation if WebRTC VAD fails for this frame
                pass

        # Energy / RMS fallback
        return self._is_speech_energy(frame)

    def _is_silence_energy(self, frame: bytes) -> bool:
        """Check if frame energy is below the configured silence threshold."""
        num_samples = len(frame) // 2
        if num_samples == 0:
            return True
        samples = struct.unpack(f"<{num_samples}h", frame)
        sum_squares = sum(s * s for s in samples)
        rms = math.sqrt(sum_squares / num_samples)
        return rms < self.config.silence_rms_threshold

    def _is_speech_energy(self, frame: bytes) -> bool:
        """Lightweight root-mean-square energy thresholding for 16-bit PCM."""
        num_samples = len(frame) // 2
        if num_samples == 0:
            return False

        # Unpack 16-bit signed integers
        samples = struct.unpack(f"<{num_samples}h", frame)
        sum_squares = sum(s * s for s in samples)
        rms = math.sqrt(sum_squares / num_samples)
        return rms > self.config.speech_rms_threshold

    def _validate_frame(self, frame: bytes) -> None:
        """Validate that frame is non-empty, bytes-like, and matches configured frame length."""
        if not isinstance(frame, (bytes, bytearray)):
            raise InvalidFrameError(f"Frame must be bytes or bytearray, got {type(frame).__name__}.")

        if len(frame) == 0:
            raise InvalidFrameError("Frame cannot be empty.")

        expected_bytes = self.config.frame_bytes
        if len(frame) != expected_bytes:
            raise InvalidFrameError(
                f"Invalid frame length: expected {expected_bytes} bytes for "
                f"{self.config.frame_duration_ms}ms at {self.config.sample_rate}Hz, got {len(frame)} bytes."
            )

    def process_frame(self, frame: bytes) -> list[VADEvent]:
        """
        Process a single audio frame, update internal state machine, and emit relevant events.
        Handles speech start triggers, buffer bounding, and end-of-speech silence thresholds cleanly.
        """
        self._validate_frame(frame)

        frame_duration = float(self.config.frame_duration_ms)
        timestamp = float(self._total_processed_frames * frame_duration)
        self._total_processed_frames += 1

        speech_detected = self.is_speech(frame)
        events: list[VADEvent] = []

        if speech_detected:
            self._silence_accumulated_ms = 0.0
            self._speech_accumulated_ms += frame_duration

            if not self._is_speech_active:
                self._pre_speech_buffer.extend(frame)
                # Bound pre-speech buffer to max_pre_speech_bytes and report truncation explicitly
                if len(self._pre_speech_buffer) > self.config.max_pre_speech_bytes:
                    excess = len(self._pre_speech_buffer) - self.config.max_pre_speech_bytes
                    del self._pre_speech_buffer[:excess]
                    if not self._is_pre_speech_truncated:
                        self._is_pre_speech_truncated = True
                        events.append(
                            VADEvent(
                                event_type="buffer_overflow",
                                timestamp_ms=timestamp,
                                frame=frame,
                                duration_ms=frame_duration,
                                truncated=True,
                                pre_speech_truncated=True,
                            )
                        )

                if self._speech_accumulated_ms >= self.config.vad_min_speech_duration_ms:
                    self._is_speech_active = True
                    # Flush pre-speech onset frames into main utterance buffer (with bounding check)
                    available = self.config.max_buffer_bytes - len(self._speech_buffer)
                    if len(self._pre_speech_buffer) <= available:
                        self._speech_buffer.extend(self._pre_speech_buffer)
                    else:
                        if available > 0:
                            self._speech_buffer.extend(self._pre_speech_buffer[:available])
                        if not self._is_truncated:
                            self._is_truncated = True
                            events.append(
                                VADEvent(
                                    event_type="buffer_overflow",
                                    timestamp_ms=timestamp,
                                    frame=frame,
                                    duration_ms=frame_duration,
                                    truncated=True,
                                    pre_speech_truncated=self._is_pre_speech_truncated,
                                )
                            )
                    self._pre_speech_buffer.clear()
                    events.append(
                        VADEvent(
                            event_type="speech_start",
                            timestamp_ms=timestamp,
                            frame=frame,
                            duration_ms=frame_duration,
                            truncated=self.is_truncated,
                            pre_speech_truncated=self._is_pre_speech_truncated,
                        )
                    )
            else:
                # Bounded buffer protection: avoid unbounded memory accumulation
                available = self.config.max_buffer_bytes - len(self._speech_buffer)
                if len(frame) <= available:
                    self._speech_buffer.extend(frame)
                else:
                    if available > 0:
                        self._speech_buffer.extend(frame[:available])
                    if not self._is_truncated:
                        self._is_truncated = True
                        events.append(
                            VADEvent(
                                event_type="buffer_overflow",
                                timestamp_ms=timestamp,
                                frame=frame,
                                duration_ms=frame_duration,
                                truncated=True,
                                pre_speech_truncated=self._is_pre_speech_truncated,
                            )
                        )

            events.append(
                VADEvent(
                    event_type="speech_frame",
                    timestamp_ms=timestamp,
                    frame=frame,
                    duration_ms=frame_duration,
                    truncated=self.is_truncated,
                    pre_speech_truncated=self._is_pre_speech_truncated,
                )
            )

        else:
            self._speech_accumulated_ms = 0.0
            if not self._is_speech_active:
                self._pre_speech_buffer.clear()
                self._is_pre_speech_truncated = False

            if self._is_speech_active:
                self._silence_accumulated_ms += frame_duration

                # Retain trailing silence frames up to the threshold for natural audio framing
                available = self.config.max_buffer_bytes - len(self._speech_buffer)
                if len(frame) <= available:
                    self._speech_buffer.extend(frame)
                else:
                    if available > 0:
                        self._speech_buffer.extend(frame[:available])
                    if not self._is_truncated:
                        self._is_truncated = True
                        events.append(
                            VADEvent(
                                event_type="buffer_overflow",
                                timestamp_ms=timestamp,
                                frame=frame,
                                duration_ms=frame_duration,
                                truncated=True,
                                pre_speech_truncated=self._is_pre_speech_truncated,
                            )
                        )

                # Check if end-of-speech silence threshold is reached
                if self._silence_accumulated_ms >= float(self.config.vad_silence_duration_ms):
                    self._is_speech_active = False
                    accumulated_audio = bytes(self._speech_buffer)
                    was_truncated = self.is_truncated
                    was_pre_speech_truncated = self._is_pre_speech_truncated
                    self._speech_buffer.clear()
                    self._silence_accumulated_ms = 0.0
                    self._is_truncated = False
                    self._is_pre_speech_truncated = False

                    events.append(
                        VADEvent(
                            event_type="speech_end",
                            timestamp_ms=timestamp,
                            frame=frame,
                            duration_ms=frame_duration,
                            accumulated_audio=accumulated_audio,
                            truncated=was_truncated,
                            pre_speech_truncated=was_pre_speech_truncated,
                        )
                    )
                else:
                    events.append(
                        VADEvent(
                            event_type="silence_frame",
                            timestamp_ms=timestamp,
                            frame=frame,
                            duration_ms=frame_duration,
                            truncated=self.is_truncated,
                            pre_speech_truncated=self._is_pre_speech_truncated,
                        )
                    )
            else:
                events.append(
                    VADEvent(
                        event_type="silence_frame",
                        timestamp_ms=timestamp,
                        frame=frame,
                        duration_ms=frame_duration,
                        truncated=False,
                        pre_speech_truncated=False,
                    )
                )

        return events

    def get_accumulated_speech(self) -> bytes:
        """Retrieve and flush currently accumulated speech audio bytes."""
        audio = bytes(self._speech_buffer)
        self._speech_buffer.clear()
        self._is_truncated = False
        self._is_pre_speech_truncated = False
        return audio

    def reset(self) -> None:
        """Reset internal state, counters, truncation flags, and volatile speech buffer."""
        self._is_speech_active = False
        self._is_truncated = False
        self._is_pre_speech_truncated = False
        self._speech_accumulated_ms = 0.0
        self._silence_accumulated_ms = 0.0
        self._speech_buffer.clear()
        self._pre_speech_buffer.clear()
        self._total_processed_frames = 0
