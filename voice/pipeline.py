"""
Voice Pipeline Orchestration Service for JARVIS Voice Subsystem (Milestone 5.1.3).
Connects audio capture, voice activity detection, and speech-to-text into a unified,
locally-executed voice input pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import queue
import threading
import time
from typing import Optional

from voice.config import VoiceConfig, default_voice_config
from voice.audio_io import AudioCaptureService, AudioDeviceError
from voice.vad import VoiceActivityDetector, VADEventType, VADEvent
from voice.stt import SpeechToTextService, TranscriptionResult

logger = logging.getLogger(__name__)


class VoicePipelineEventType(str, Enum):
    """Supported voice pipeline event types."""
    LISTENING_STARTED = "listening_started"
    SPEECH_STARTED = "speech_started"
    SPEECH_DETECTED = "speech_detected"
    TRANSCRIPTION_COMPLETED = "transcription_completed"
    TRANSCRIPTION_FAILED = "transcription_failed"
    BUFFER_OVERFLOW = "buffer_overflow"
    PIPELINE_ERROR = "pipeline_error"
    LISTENING_STOPPED = "listening_stopped"


CRITICAL_EVENT_TYPES = {
    VoicePipelineEventType.LISTENING_STOPPED.value,
    VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
    VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
    VoicePipelineEventType.PIPELINE_ERROR.value,
}


@dataclass(frozen=True)
class VoicePipelineEvent:
    """
    Structured event emitted by the VoicePipeline.

    Attributes:
        event_type: Type of the event (matches VoicePipelineEventType string value).
        timestamp: Unix timestamp in seconds (from time.time()) when the event was generated.
        transcription: TranscriptionResult when event_type is TRANSCRIPTION_COMPLETED or TRANSCRIPTION_FAILED.
        error: Descriptive error message for error or failure events.
        audio_duration_seconds: Input audio duration in seconds, if applicable.
        truncated: True if audio buffer overflow or onset frame truncation was detected.
        vad_timestamp_ms: Optional elapsed VAD frame timestamp in milliseconds, if applicable.
    """
    event_type: str
    timestamp: float
    transcription: Optional[TranscriptionResult] = None
    error: Optional[str] = None
    audio_duration_seconds: Optional[float] = None
    truncated: Optional[bool] = None
    vad_timestamp_ms: Optional[float] = None


class VoicePipelineError(Exception):
    """Exception raised for lifecycle or operational failures in VoicePipeline."""
    pass


class VoicePipeline:
    """
    Orchestrates continuous audio capture, voice activity detection, and speech-to-text.
    Operates strictly locally with thread-safe lifecycle controls and bounded memory.
    """

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        capture_service: Optional[AudioCaptureService] = None,
        vad: Optional[VoiceActivityDetector] = None,
        stt_service: Optional[SpeechToTextService] = None,
    ) -> None:
        """
        Initialize the VoicePipeline with injectable components.

        Args:
            config: Shared VoiceConfig. Defaults to default_voice_config.
            capture_service: AudioCaptureService instance. Constructed if None.
            vad: VoiceActivityDetector instance. Constructed if None.
            stt_service: SpeechToTextService instance. Constructed if None.
        """
        self.config = config or default_voice_config
        self.capture_service = capture_service or AudioCaptureService(config=self.config)
        self.vad = vad or VoiceActivityDetector(config=self.config)
        self.stt_service = stt_service or SpeechToTextService(config=self.config)

        # Threading and lifecycle synchronization
        self._lifecycle_lock = threading.RLock()
        self._stt_lock = threading.Lock()
        self._running: bool = False
        self._is_stopping: bool = False
        self._session_id: int = 0
        self._stop_event: threading.Event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Bounded event delivery queue
        self._event_queue: queue.Queue[VoicePipelineEvent] = queue.Queue(
            maxsize=self.config.pipeline_event_queue_size
        )

    @property
    def is_running(self) -> bool:
        """Check whether the pipeline is currently active and processing audio."""
        with self._lifecycle_lock:
            return self._running

    def start(self, clear_capture_buffer: bool = True) -> None:
        """
        Start the voice processing pipeline and background worker.
        Idempotent if already running.

        Args:
            clear_capture_buffer: If True (default), flushes volatile frames currently in
                the capture service queue. Set to False when handoff audio frames from a
                preceding component (such as wake-word detection) should be preserved.

        Raises:
            VoicePipelineError: If a previous shutdown is still finalizing or worker fails to start.
        """
        with self._lifecycle_lock:
            if self._is_stopping:
                raise VoicePipelineError(
                    "Cannot start pipeline: previous pipeline session is still finalizing shutdown."
                )

            if self._running:
                logger.debug("VoicePipeline is already running; start() ignored.")
                return

            if self._worker_thread is not None and self._worker_thread.is_alive():
                raise VoicePipelineError(
                    "Cannot start pipeline: worker thread from previous session is still alive."
                )

            # Reset VAD and conditionally clear stale capture frames
            self.vad.reset()
            if clear_capture_buffer:
                self.capture_service.clear_buffer()

            # Start audio capture service
            try:
                self.capture_service.start()
            except Exception as e:
                logger.error("Failed to start audio capture service: %s", e)
                self._publish_event(
                    VoicePipelineEvent(
                        event_type=VoicePipelineEventType.PIPELINE_ERROR.value,
                        timestamp=time.time(),
                        error=f"Capture service startup failed: {e}",
                    )
                )
                raise VoicePipelineError(f"Failed to start capture service: {e}") from e

            self._session_id += 1
            session_id = self._session_id
            self._stop_event.clear()
            self._running = True

            self._worker_thread = threading.Thread(
                target=self._processing_worker,
                args=(session_id,),
                name=f"VoicePipelineWorker-{session_id}",
                daemon=True,
            )
            self._worker_thread.start()

            self._publish_event(
                VoicePipelineEvent(
                    event_type=VoicePipelineEventType.LISTENING_STARTED.value,
                    timestamp=time.time(),
                )
            )
            logger.info("VoicePipeline started (session=%d)", session_id)

    def stop(self) -> None:
        """
        Stop the voice processing pipeline and join the worker thread.
        Idempotent if already stopped.

        Raises:
            VoicePipelineError: If the background worker fails to terminate within the timeout.
        """
        worker_to_join: Optional[threading.Thread] = None
        session_to_stop: int = 0

        with self._lifecycle_lock:
            worker_alive = self._worker_thread is not None and self._worker_thread.is_alive()
            if not self._running and not self._is_stopping and not worker_alive:
                logger.debug("VoicePipeline is already stopped; stop() ignored.")
                return

            if self._is_stopping:
                logger.debug("VoicePipeline shutdown is already in progress.")
                return

            self._is_stopping = True
            self._running = False
            self._stop_event.set()
            worker_to_join = self._worker_thread
            session_to_stop = self._session_id

        # Stop audio capture outside lifecycle lock to avoid deadlocks
        try:
            self.capture_service.stop()
        except Exception as e:
            logger.warning("Error stopping capture service during pipeline shutdown: %s", e)

        # Join worker thread without holding lifecycle lock
        worker_exited = True
        if worker_to_join is not None and worker_to_join.is_alive():
            worker_to_join.join(timeout=self.config.pipeline_join_timeout_seconds)
            if worker_to_join.is_alive():
                worker_exited = False
                logger.error(
                    "VoicePipeline worker %s failed to terminate within %.1fs timeout",
                    worker_to_join.name,
                    self.config.pipeline_join_timeout_seconds,
                )

        with self._lifecycle_lock:
            if self._session_id == session_to_stop:
                self._is_stopping = False
                if worker_exited:
                    self._worker_thread = None
                    self.vad.reset()
                    self.capture_service.clear_buffer()
                    self._publish_event(
                        VoicePipelineEvent(
                            event_type=VoicePipelineEventType.LISTENING_STOPPED.value,
                            timestamp=time.time(),
                        )
                    )
                    logger.info("VoicePipeline stopped cleanly (session=%d)", session_to_stop)
                else:
                    raise VoicePipelineError("Pipeline worker thread failed to terminate cleanly.")

    def read_event(self, timeout: Optional[float] = 0.1) -> Optional[VoicePipelineEvent]:
        """
        Retrieve the next event from the bounded event queue.

        Args:
            timeout: Maximum seconds to wait for an event. None for non-blocking.

        Returns:
            VoicePipelineEvent if available, None if queue timed out or is empty.
        """
        try:
            if timeout is None or timeout <= 0:
                return self._event_queue.get_nowait()
            return self._event_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def clear_events(self) -> int:
        """
        Drain and clear all pending events from the event queue.

        Returns:
            int: The exact number of events removed from the queue.
        """
        cleared_count = 0
        while True:
            try:
                self._event_queue.get_nowait()
                cleared_count += 1
            except queue.Empty:
                break
        return cleared_count

    def _publish_event(self, event: VoicePipelineEvent) -> None:
        """
        Publish a structured event to the bounded event queue with critical event protection.

        If the event queue is full:
          1. Priority retention: Ordinary events (e.g. speech_started, buffer_overflow,
             listening_started) are discarded before critical events. The oldest ordinary
             event in the queue is dropped to make room for the new event.
          2. All-critical fallback: If all events currently in the queue are critical:
             - If the incoming event is ordinary, the incoming event is dropped to protect
               the queued critical events.
             - If the incoming event is critical, the oldest critical event in the queue is
               dropped (deterministic FIFO fallback for critical events) to enqueue the new critical event.
          3. Non-blocking invariant: Enqueue and eviction operations acquire the queue's
             internal mutex and never block or wait, ensuring the worker thread is never stalled.
        """
        with self._event_queue.mutex:
            if self._event_queue._qsize() < self._event_queue.maxsize:
                self._event_queue._put(event)
                self._event_queue.unfinished_tasks += 1
                self._event_queue.not_empty.notify()
                return

            is_incoming_critical = event.event_type in CRITICAL_EVENT_TYPES

            # Find oldest non-critical (ordinary) event to drop
            target_to_drop = None
            for item in self._event_queue.queue:
                if item.event_type not in CRITICAL_EVENT_TYPES:
                    target_to_drop = item
                    break

            if target_to_drop is not None:
                self._event_queue.queue.remove(target_to_drop)
                self._event_queue.unfinished_tasks -= 1
                logger.warning(
                    "VoicePipeline event queue overflow; dropped oldest ordinary event '%s' to make room for '%s'",
                    target_to_drop.event_type,
                    event.event_type,
                )
                self._event_queue._put(event)
                self._event_queue.unfinished_tasks += 1
                self._event_queue.not_empty.notify()
            else:
                # All queued events are critical
                if not is_incoming_critical:
                    logger.warning(
                        "VoicePipeline event queue is full of critical events; dropped incoming ordinary event '%s'",
                        event.event_type,
                    )
                    return
                else:
                    dropped = self._event_queue.queue.popleft()
                    self._event_queue.unfinished_tasks -= 1
                    logger.warning(
                        "VoicePipeline event queue full of critical events; dropped oldest critical event '%s' to enqueue newest '%s'",
                        dropped.event_type,
                        event.event_type,
                    )
                    self._event_queue._put(event)
                    self._event_queue.unfinished_tasks += 1
                    self._event_queue.not_empty.notify()

    def _processing_worker(self, session_id: int) -> None:
        """
        Dedicated processing worker loop.
        Reads frames from AudioCaptureService, processes through VAD, and triggers STT.
        """
        logger.debug("VoicePipeline processing worker started (session=%d)", session_id)
        current_utterance_overflow_emitted = False
        consecutive_read_errors = 0

        try:
            while not self._stop_event.is_set():
                # Session validity check: exit if superseded by a newer session
                with self._lifecycle_lock:
                    if self._session_id != session_id:
                        logger.debug(
                            "VoicePipeline worker session %d superseded by %d; exiting.",
                            session_id,
                            self._session_id,
                        )
                        return

                # Read PCM frame from capture service
                try:
                    frame = self.capture_service.read_frame(
                        timeout=self.config.pipeline_frame_read_timeout_seconds
                    )
                    consecutive_read_errors = 0
                except Exception as e:
                    consecutive_read_errors += 1
                    logger.warning(
                        "Capture read error (%d/%d): %s",
                        consecutive_read_errors,
                        self.config.pipeline_max_consecutive_read_errors,
                        e,
                    )
                    if consecutive_read_errors >= self.config.pipeline_max_consecutive_read_errors:
                        logger.error(
                            "Maximum consecutive capture read errors (%d) reached; stopping worker cleanly.",
                            self.config.pipeline_max_consecutive_read_errors,
                        )
                        self._publish_event(
                            VoicePipelineEvent(
                                event_type=VoicePipelineEventType.PIPELINE_ERROR.value,
                                timestamp=time.time(),
                                error=(
                                    f"Maximum consecutive capture read errors "
                                    f"({self.config.pipeline_max_consecutive_read_errors}) exceeded: {e}"
                                ),
                            )
                        )
                        break

                    self._publish_event(
                        VoicePipelineEvent(
                            event_type=VoicePipelineEventType.PIPELINE_ERROR.value,
                            timestamp=time.time(),
                            error=f"Capture read error ({consecutive_read_errors}/{self.config.pipeline_max_consecutive_read_errors}): {e}",
                        )
                    )
                    # Avoid tight busy loop on transient failures
                    self._stop_event.wait(0.02)
                    continue

                if self._stop_event.is_set():
                    break

                if frame is None:
                    # Timeout occurred with no frame; loop and check stop_event
                    continue

                # Validate frame size
                if len(frame) != self.config.frame_bytes:
                    logger.warning(
                        "Invalid frame length received: %d bytes (expected %d)",
                        len(frame),
                        self.config.frame_bytes,
                    )
                    self._publish_event(
                        VoicePipelineEvent(
                            event_type=VoicePipelineEventType.PIPELINE_ERROR.value,
                            timestamp=time.time(),
                            error=f"Invalid frame length: {len(frame)} bytes (expected {self.config.frame_bytes})",
                        )
                    )
                    continue

                # Process frame through VoiceActivityDetector
                try:
                    vad_events = self.vad.process_frame(frame)
                except Exception as e:
                    logger.error("VAD processing error: %s", e)
                    self._publish_event(
                        VoicePipelineEvent(
                            event_type=VoicePipelineEventType.PIPELINE_ERROR.value,
                            timestamp=time.time(),
                            error=f"VAD processing error: {e}",
                        )
                    )
                    continue

                # Handle emitted VAD events
                for v_event in vad_events:
                    vad_ms = getattr(v_event, "timestamp_ms", None)

                    if v_event.event_type == "buffer_overflow":
                        if not current_utterance_overflow_emitted:
                            current_utterance_overflow_emitted = True
                            self._publish_event(
                                VoicePipelineEvent(
                                    event_type=VoicePipelineEventType.BUFFER_OVERFLOW.value,
                                    timestamp=time.time(),
                                    error="Utterance truncated: audio buffer overflow.",
                                    truncated=True,
                                    vad_timestamp_ms=vad_ms,
                                )
                            )

                    elif v_event.event_type == "speech_start":
                        current_utterance_overflow_emitted = False
                        if getattr(v_event, "pre_speech_truncated", False):
                            current_utterance_overflow_emitted = True
                            self._publish_event(
                                VoicePipelineEvent(
                                    event_type=VoicePipelineEventType.BUFFER_OVERFLOW.value,
                                    timestamp=time.time(),
                                    error="Pre-speech buffer truncated: onset audio was discarded.",
                                    truncated=True,
                                    vad_timestamp_ms=vad_ms,
                                )
                            )
                        self._publish_event(
                            VoicePipelineEvent(
                                event_type=VoicePipelineEventType.SPEECH_STARTED.value,
                                timestamp=time.time(),
                                truncated=getattr(v_event, "pre_speech_truncated", False),
                                vad_timestamp_ms=vad_ms,
                            )
                        )

                    elif v_event.event_type == "speech_end":
                        # Truncation policy: discard truncated audio rather than transcribing incomplete utterance
                        is_truncated = (
                            getattr(v_event, "truncated", False)
                            or getattr(v_event, "pre_speech_truncated", False)
                            or current_utterance_overflow_emitted
                        )
                        current_utterance_overflow_emitted = False

                        if is_truncated:
                            logger.warning(
                                "Truncated utterance completed; discarding audio without calling STT per truncation policy."
                            )
                            continue

                        accumulated_audio = v_event.accumulated_audio
                        if not accumulated_audio or len(accumulated_audio) == 0:
                            logger.debug("Speech ended with empty accumulated audio; skipping STT.")
                            continue

                        duration_sec = len(accumulated_audio) / (
                            self.config.sample_rate * self.config.bytes_per_sample
                        )

                        # Check local STT availability before transcription
                        try:
                            stt_available = self.stt_service.is_available()
                        except Exception as avail_err:
                            logger.error("STT availability check error: %s", avail_err)
                            self._publish_event(
                                VoicePipelineEvent(
                                    event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
                                    timestamp=time.time(),
                                    error=f"STT availability check error: {avail_err}",
                                    audio_duration_seconds=duration_sec,
                                    truncated=False,
                                    vad_timestamp_ms=vad_ms,
                                )
                            )
                            continue

                        if not stt_available:
                            logger.warning("Local STT service is unavailable; skipping transcription.")
                            self._publish_event(
                                VoicePipelineEvent(
                                    event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
                                    timestamp=time.time(),
                                    error="Speech-to-text service is unavailable: local Whisper executable or model file was not found.",
                                    audio_duration_seconds=duration_sec,
                                    truncated=False,
                                    vad_timestamp_ms=vad_ms,
                                )
                            )
                            continue

                        # Ensure non-overlapping STT calls
                        stt_result: Optional[TranscriptionResult] = None
                        with self._stt_lock:
                            if self._stop_event.is_set():
                                # If stopped while waiting for STT lock, check session
                                with self._lifecycle_lock:
                                    if self._session_id != session_id:
                                        return

                            try:
                                stt_result = self.stt_service.transcribe(accumulated_audio)
                            except Exception as stt_err:
                                logger.error("Unexpected STT exception during transcription: %s", stt_err)
                                self._publish_event(
                                    VoicePipelineEvent(
                                        event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
                                        timestamp=time.time(),
                                        error=f"STT execution error: {type(stt_err).__name__}: {stt_err}",
                                        audio_duration_seconds=duration_sec,
                                        truncated=False,
                                        vad_timestamp_ms=vad_ms,
                                    )
                                )
                                continue

                        # Verify session ownership before publishing result
                        with self._lifecycle_lock:
                            if self._session_id != session_id:
                                return

                        if stt_result is not None:
                            if stt_result.success:
                                self._publish_event(
                                    VoicePipelineEvent(
                                        event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
                                        timestamp=time.time(),
                                        transcription=stt_result,
                                        audio_duration_seconds=duration_sec,
                                        truncated=False,
                                        vad_timestamp_ms=vad_ms,
                                    )
                                )
                            else:
                                self._publish_event(
                                    VoicePipelineEvent(
                                        event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
                                        timestamp=time.time(),
                                        transcription=stt_result,
                                        error=stt_result.error or "Transcription failed",
                                        audio_duration_seconds=duration_sec,
                                        truncated=False,
                                        vad_timestamp_ms=vad_ms,
                                    )
                                )
        finally:
            if consecutive_read_errors >= self.config.pipeline_max_consecutive_read_errors:
                try:
                    self.capture_service.stop()
                except Exception as stop_err:
                    logger.debug("Error stopping capture service after read errors: %s", stop_err)
                with self._lifecycle_lock:
                    if self._session_id == session_id:
                        self._running = False

            logger.debug("VoicePipeline processing worker exited (session=%d)", session_id)
