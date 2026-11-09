"""
Voice Activation Orchestrator for JARVIS Voice Subsystem (Milestone 5.1.5).
Coordinates WakeWordDetector, AudioCaptureService, VoicePipeline, and local Whisper STT
into a continuous, wake-word-driven voice interaction cycle.
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
from voice.audio_io import AudioCaptureService
from voice.wake_word import WakeWordDetector, WakeWordResult
from voice.pipeline import VoicePipeline, VoicePipelineEventType
from voice.stt import TranscriptionResult

logger = logging.getLogger(__name__)


class VoiceActivationEventType(str, Enum):
    """Supported voice activation event types."""
    LISTENING_STARTED = "listening_started"
    WAKE_WORD_DETECTED = "wake_word_detected"
    COMMAND_LISTENING_STARTED = "command_listening_started"
    COMMAND_TRANSCRIBED = "command_transcribed"
    COMMAND_FAILED = "command_failed"
    ACTIVATION_ERROR = "activation_error"
    LISTENING_STOPPED = "listening_stopped"


CRITICAL_ACTIVATION_EVENT_TYPES = {
    VoiceActivationEventType.LISTENING_STOPPED.value,
    VoiceActivationEventType.COMMAND_TRANSCRIBED.value,
    VoiceActivationEventType.COMMAND_FAILED.value,
    VoiceActivationEventType.ACTIVATION_ERROR.value,
}


@dataclass(frozen=True)
class VoiceActivationEvent:
    """
    Structured event emitted by the VoiceActivationOrchestrator.

    Attributes:
        event_type: Type of the event (matches VoiceActivationEventType string value).
        timestamp: Unix timestamp in seconds (time.time()) when event was created.
        transcription: Optional TranscriptionResult when event_type is COMMAND_TRANSCRIBED or COMMAND_FAILED.
        wake_word_result: Optional WakeWordResult when event_type is WAKE_WORD_DETECTED.
        error: Descriptive error message for error or failure events.
        audio_duration_seconds: Duration of recorded command audio in seconds, if applicable.
    """
    event_type: str
    timestamp: float
    transcription: Optional[TranscriptionResult] = None
    wake_word_result: Optional[WakeWordResult] = None
    error: Optional[str] = None
    audio_duration_seconds: Optional[float] = None


class VoiceActivationError(Exception):
    """Exception raised for lifecycle, configuration, or operational errors in VoiceActivationOrchestrator."""
    pass


class VoiceActivationOrchestrator:
    """
    Coordinates WakeWordDetector, AudioCaptureService, and VoicePipeline into a continuous,
    wake-word-driven voice interaction cycle.

    Guarantees:
      - Single microphone reader: detector and pipeline never consume frames simultaneously.
      - Component identity: VoicePipeline and Orchestrator strictly share the same AudioCaptureService instance.
      - Local-only execution: Zero cloud dependencies, zero network requests, zero Whisper calls during standby.
      - Safe concurrency: Non-deadlocking worker joins, monotonic session tracking, and idempotent lifecycle controls.
    """

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        capture_service: Optional[AudioCaptureService] = None,
        wake_word_detector: Optional[WakeWordDetector] = None,
        voice_pipeline: Optional[VoicePipeline] = None,
    ) -> None:
        """
        Initialize the VoiceActivationOrchestrator with configurable or injectable components.

        Args:
            config: Shared VoiceConfig. Defaults to default_voice_config.
            capture_service: AudioCaptureService instance.
            wake_word_detector: WakeWordDetector instance.
            voice_pipeline: VoicePipeline instance.

        Raises:
            VoiceActivationError: If injected voice_pipeline and capture_service do not share the exact same instance.
        """
        self.config = config or default_voice_config

        # 1. Enforce strict single-capture component wiring
        if voice_pipeline is not None and capture_service is not None:
            if voice_pipeline.capture_service is not capture_service:
                raise VoiceActivationError(
                    "Incompatible component wiring: voice_pipeline must use the exact same "
                    "AudioCaptureService instance as the orchestrator."
                )

        if voice_pipeline is not None:
            self.voice_pipeline = voice_pipeline
            self.capture_service = voice_pipeline.capture_service
        elif capture_service is not None:
            self.capture_service = capture_service
            self.voice_pipeline = VoicePipeline(
                config=self.config,
                capture_service=self.capture_service,
            )
        else:
            self.capture_service = AudioCaptureService(config=self.config)
            self.voice_pipeline = VoicePipeline(
                config=self.config,
                capture_service=self.capture_service,
            )

        if wake_word_detector is not None:
            self.wake_word_detector = wake_word_detector
            self._owns_detector = False
        else:
            self.wake_word_detector = WakeWordDetector(config=self.config)
            self._owns_detector = True

        # Threading and lifecycle synchronization
        self._lifecycle_lock = threading.RLock()
        self._running: bool = False
        self._is_stopping: bool = False
        self._pipeline_shutdown_failed: bool = False
        self._session_id: int = 0
        self._stop_event: threading.Event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Bounded event delivery queue with critical event priority
        self._event_queue: queue.Queue[VoiceActivationEvent] = queue.Queue(
            maxsize=self.config.activation_event_queue_size
        )

    @property
    def is_running(self) -> bool:
        """Check whether the orchestrator is currently active."""
        with self._lifecycle_lock:
            return (
                self._running
                and not self._is_stopping
                and (self._worker_thread is not None and self._worker_thread.is_alive())
            )

    def start(self) -> None:
        """
        Start the voice activation orchestrator and background coordination worker.
        Idempotent if already running.

        Raises:
            VoiceActivationError: If previous shutdown is still finalizing or components fail to start.
        """
        with self._lifecycle_lock:
            if self._is_stopping:
                raise VoiceActivationError(
                    "Cannot start orchestrator: previous session is still finalizing shutdown."
                )

            if self._running:
                logger.debug("VoiceActivationOrchestrator is already running; start() ignored.")
                return

            if self._worker_thread is not None and self._worker_thread.is_alive():
                raise VoiceActivationError(
                    "Cannot start orchestrator: worker thread from previous session is still alive."
                )

            # 2. Verify wake-word detector availability before entering listening loop
            if not self.wake_word_detector.is_available():
                err_msg = (
                    f"Wake-word detector is unavailable: "
                    f"{self.wake_word_detector.initialization_error or 'Engine or model not ready.'}"
                )
                logger.error(err_msg)
                self._publish_event(
                    VoiceActivationEvent(
                        event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                        timestamp=time.time(),
                        error=err_msg,
                    )
                )
                raise VoiceActivationError(err_msg)

            # Reset detector and clear stale capture frames
            self.wake_word_detector.reset()
            self.capture_service.clear_buffer()

            # 3. Clear initial pipeline events (fail closed if queue drainage fails)
            try:
                self.voice_pipeline.clear_events()
            except Exception as clear_err:
                logger.error("Failed to clear initial pipeline events during startup: %s", clear_err)
                self._publish_event(
                    VoiceActivationEvent(
                        event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                        timestamp=time.time(),
                        error=f"Initial pipeline event drainage failed: {clear_err}",
                    )
                )
                raise VoiceActivationError(f"Initial pipeline event drainage failed: {clear_err}") from clear_err

            # 4. Start AudioCaptureService
            try:
                self.capture_service.start()
            except Exception as e:
                logger.error("Failed to start audio capture service: %s", e)
                self._publish_event(
                    VoiceActivationEvent(
                        event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                        timestamp=time.time(),
                        error=f"Capture service startup failed: {e}",
                    )
                )
                raise VoiceActivationError(f"Capture service startup failed: {e}") from e

            self._session_id += 1
            session_id = self._session_id
            self._stop_event.clear()
            self._pipeline_shutdown_failed = False
            self._running = True

            self._worker_thread = threading.Thread(
                target=self._orchestrator_worker,
                args=(session_id,),
                name=f"VoiceActivationWorker-{session_id}",
                daemon=True,
            )
            self._worker_thread.start()

            self._publish_event(
                VoiceActivationEvent(
                    event_type=VoiceActivationEventType.LISTENING_STARTED.value,
                    timestamp=time.time(),
                )
            )
            logger.info("VoiceActivationOrchestrator started (session=%d)", session_id)

    def stop(self) -> None:
        """
        Stop the voice activation orchestrator and cleanly join the worker thread.
        Idempotent if already stopped.

        Raises:
            VoiceActivationError: If the background worker fails to terminate within the timeout.
        """
        worker_to_join: Optional[threading.Thread] = None
        session_to_stop: int = 0

        with self._lifecycle_lock:
            if self._pipeline_shutdown_failed:
                raise VoiceActivationError("VoicePipeline failed to terminate cleanly during shutdown.")

            if not self._running and not self._is_stopping:
                logger.debug("VoiceActivationOrchestrator is already stopped; stop() ignored.")
                return

            if self._is_stopping:
                logger.debug("VoiceActivationOrchestrator shutdown is already in progress.")
                return

            self._is_stopping = True
            self._running = False
            self._stop_event.set()
            worker_to_join = self._worker_thread
            session_to_stop = self._session_id

        # 1. Stop voice pipeline if running, outside lifecycle lock to avoid deadlocks
        pipeline_stopped_cleanly = not self._pipeline_shutdown_failed
        try:
            if self._is_pipeline_active():
                self.voice_pipeline.stop()
        except Exception as e:
            pipeline_stopped_cleanly = False
            self._pipeline_shutdown_failed = True
            logger.warning("Error stopping voice pipeline during orchestrator shutdown: %s", e)

        # 2. Stop audio capture service outside lifecycle lock
        try:
            self.capture_service.stop()
        except Exception as e:
            logger.warning("Error stopping capture service during orchestrator shutdown: %s", e)

        # 3. Join worker thread without holding lifecycle lock
        worker_exited = True
        if worker_to_join is not None and worker_to_join.is_alive():
            worker_to_join.join(timeout=self.config.activation_join_timeout_seconds)
            if worker_to_join.is_alive():
                worker_exited = False
                logger.error(
                    "VoiceActivationOrchestrator worker %s failed to terminate within %.1fs timeout",
                    worker_to_join.name,
                    self.config.activation_join_timeout_seconds,
                )

        # 4. Finalize state
        with self._lifecycle_lock:
            if self._session_id == session_to_stop:
                self._is_stopping = False
                if (
                    worker_exited
                    and pipeline_stopped_cleanly
                    and not self._is_pipeline_active()
                    and not self._pipeline_shutdown_failed
                ):
                    self._worker_thread = None
                    self.wake_word_detector.reset()
                    self.capture_service.clear_buffer()
                    if self._owns_detector:
                        try:
                            self.wake_word_detector.stop()
                        except Exception as e:
                            logger.debug("Error stopping owned wake-word detector: %s", e)
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.LISTENING_STOPPED.value,
                            timestamp=time.time(),
                        )
                    )
                    logger.info("VoiceActivationOrchestrator stopped cleanly (session=%d)", session_to_stop)
                else:
                    err_msg = "VoiceActivationOrchestrator worker or voice pipeline failed to terminate cleanly."
                    if (
                        not pipeline_stopped_cleanly
                        or self._is_pipeline_active()
                        or self._pipeline_shutdown_failed
                    ):
                        err_msg = "VoicePipeline failed to terminate cleanly during shutdown."
                    elif not worker_exited:
                        err_msg = "VoiceActivationOrchestrator worker thread failed to terminate cleanly."
                    raise VoiceActivationError(err_msg)

    def _is_pipeline_active(self) -> bool:
        """
        Check whether the voice pipeline is currently active or has an alive worker thread.
        Uses exclusively the documented public is_running and worker_alive properties.
        Fails closed (returns True) if either property cannot be read reliably.
        """
        try:
            is_running = getattr(self.voice_pipeline, "is_running")
            if callable(is_running):
                is_running = is_running()
            if bool(is_running):
                return True

            worker_alive = getattr(self.voice_pipeline, "worker_alive")
            if callable(worker_alive):
                worker_alive = worker_alive()
            return bool(worker_alive)
        except Exception as exc:
            logger.warning(
                "Failed to read pipeline lifecycle properties reliably; failing closed as active: %s",
                exc,
            )
            return True

    def read_event(self, timeout: Optional[float] = 0.1) -> Optional[VoiceActivationEvent]:
        """
        Retrieve the next event from the bounded event queue.

        Args:
            timeout: Maximum seconds to wait for an event. None or <=0 for non-blocking.

        Returns:
            VoiceActivationEvent if available, None if queue timed out or is empty.
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

    def _publish_event(self, event: VoiceActivationEvent) -> None:
        """
        Publish a structured event to the bounded event queue with critical event protection.

        If the event queue is full:
          1. Priority retention: Ordinary events (e.g. listening_started, wake_word_detected,
             command_listening_started) are discarded before critical events. The oldest ordinary
             event is dropped to make room for the new event.
          2. All-critical fallback: If all queued events are critical:
             - If incoming event is ordinary, incoming event is dropped.
             - If incoming event is critical, oldest critical event is dropped (FIFO fallback).
          3. Non-blocking invariant: Enqueue and eviction never block worker threads.
        """
        with self._event_queue.mutex:
            if self._event_queue._qsize() < self._event_queue.maxsize:
                self._event_queue._put(event)
                self._event_queue.unfinished_tasks += 1
                self._event_queue.not_empty.notify()
                return

            is_incoming_critical = event.event_type in CRITICAL_ACTIVATION_EVENT_TYPES

            # Find oldest non-critical (ordinary) event to drop
            target_to_drop = None
            for item in self._event_queue.queue:
                if item.event_type not in CRITICAL_ACTIVATION_EVENT_TYPES:
                    target_to_drop = item
                    break

            if target_to_drop is not None:
                self._event_queue.queue.remove(target_to_drop)
                self._event_queue.unfinished_tasks -= 1
                logger.warning(
                    "VoiceActivationOrchestrator queue overflow; dropped ordinary event '%s' to make room for '%s'",
                    target_to_drop.event_type,
                    event.event_type,
                )
                self._event_queue._put(event)
                self._event_queue.unfinished_tasks += 1
                self._event_queue.not_empty.notify()
            else:
                # All queued events are critical
                if is_incoming_critical:
                    # Drop oldest critical event to enqueue new critical event
                    dropped = self._event_queue.queue.popleft() if hasattr(self._event_queue.queue, "popleft") else self._event_queue.queue.pop(0)
                    self._event_queue.unfinished_tasks -= 1
                    logger.warning(
                        "VoiceActivationOrchestrator queue full of critical events; dropped oldest critical '%s' for '%s'",
                        dropped.event_type,
                        event.event_type,
                    )
                    self._event_queue._put(event)
                    self._event_queue.unfinished_tasks += 1
                    self._event_queue.not_empty.notify()
                else:
                    # Incoming ordinary event dropped to preserve queued critical events
                    logger.warning(
                        "VoiceActivationOrchestrator queue full of critical events; dropped incoming ordinary event '%s'",
                        event.event_type,
                    )

    def _orchestrator_worker(self, session_id: int) -> None:
        """
        Continuous background coordination loop.
        Monitors microphone audio for wake word during standby, handoffs audio frames
        to VoicePipeline upon detection, and returns to standby after transcription.
        """
        logger.debug("VoiceActivationOrchestrator worker started (session=%d)", session_id)

        try:
            while not self._stop_event.is_set():
                with self._lifecycle_lock:
                    if self._session_id != session_id:
                        return

                # 1. Ensure audio capture service is active
                try:
                    if not self.capture_service.is_running:
                        self.capture_service.clear_buffer()
                        self.capture_service.start()
                except Exception as e:
                    logger.error("Capture startup error in worker: %s", e)
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                            timestamp=time.time(),
                            error=f"Capture service startup failed: {e}",
                        )
                    )
                    break

                # 2. Read audio frame for wake-word detector
                try:
                    frame = self.capture_service.read_frame(
                        timeout=self.config.activation_frame_read_timeout_seconds
                    )
                except Exception as e:
                    logger.error("Capture read failure in worker: %s", e)
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                            timestamp=time.time(),
                            error=f"Capture read failure: {e}",
                        )
                    )
                    break

                if frame is None:
                    continue

                if self._stop_event.is_set():
                    break

                # 3. Process frame through wake-word detector
                try:
                    ww_res = self.wake_word_detector.process_frame(frame)
                except Exception as e:
                    logger.error("Wake-word detector exception in worker: %s", e)
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                            timestamp=time.time(),
                            error=f"Wake-word detector exception: {type(e).__name__}: {e}",
                        )
                    )
                    break

                if not ww_res.detected:
                    continue

                # 4. Wake word detected! Publish notification event
                now = time.time()
                self._publish_event(
                    VoiceActivationEvent(
                        event_type=VoiceActivationEventType.WAKE_WORD_DETECTED.value,
                        timestamp=now,
                        wake_word_result=ww_res,
                    )
                )

                # 5. Clear stale pipeline events before starting new pipeline session
                try:
                    self.voice_pipeline.clear_events()
                except Exception as clear_err:
                    logger.error("Failed to clear stale pipeline events after wake-word detection: %s", clear_err)
                    self._pipeline_shutdown_failed = True
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                            timestamp=time.time(),
                            error=f"Per-cycle pipeline event drainage failed: {clear_err}",
                        )
                    )
                    try:
                        self.capture_service.stop()
                    except Exception as cap_err:
                        logger.error("Error stopping capture service after event drainage failure: %s", cap_err)
                    break

                # 6. Transfer frame consumption to VoicePipeline (preserving post-wake capture frames)
                try:
                    self.voice_pipeline.start(clear_capture_buffer=False)
                except Exception as start_err:
                    logger.error("VoicePipeline startup failure: %s", start_err)
                    cleanup_succeeded = True
                    try:
                        self.voice_pipeline.stop()
                    except Exception as stop_err:
                        cleanup_succeeded = False
                        logger.error("VoicePipeline cleanup failed after startup error: %s", stop_err)

                    if not cleanup_succeeded or self._is_pipeline_active():
                        logger.critical(
                            "VoicePipeline failed to terminate cleanly after startup failure; "
                            "halting activation orchestrator to prevent competing microphone consumers."
                        )
                        self._pipeline_shutdown_failed = True
                        self._publish_event(
                            VoiceActivationEvent(
                                event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                                timestamp=time.time(),
                                error=f"VoicePipeline startup failure ({start_err}) and cleanup failed to terminate cleanly",
                            )
                        )
                        try:
                            self.capture_service.stop()
                        except Exception as cap_err:
                            logger.error(
                                "Error stopping capture service after pipeline startup cleanup failure: %s", cap_err
                            )
                        break

                    # Cleanup succeeded and pipeline confirmed stopped: publish COMMAND_FAILED, reset detector, resume standby
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                            timestamp=time.time(),
                            error=f"VoicePipeline startup failure: {start_err}",
                        )
                    )
                    self.wake_word_detector.reset()
                    continue

                # 7. Publish COMMAND_LISTENING_STARTED strictly after successful pipeline startup
                self._publish_event(
                    VoiceActivationEvent(
                        event_type=VoiceActivationEventType.COMMAND_LISTENING_STARTED.value,
                        timestamp=time.time(),
                    )
                )

                # 8. Consume events from VoicePipeline until a terminal event is received
                pipeline_active = True
                while pipeline_active and not self._stop_event.is_set():
                    with self._lifecycle_lock:
                        if self._session_id != session_id:
                            pipeline_active = False
                            break

                    try:
                        pipe_event = self.voice_pipeline.read_event(
                            timeout=self.config.activation_pipeline_event_timeout_seconds
                        )
                    except Exception as e:
                        logger.error("Error reading pipeline event: %s", e)
                        self._publish_event(
                            VoiceActivationEvent(
                                event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                                timestamp=time.time(),
                                error=f"Pipeline event read error: {e}",
                            )
                        )
                        break

                    if pipe_event is None:
                        # Check if pipeline stopped unexpectedly
                        if not self.voice_pipeline.is_running:
                            # Drain any remaining terminal events
                            while True:
                                remaining = self.voice_pipeline.read_event(timeout=0.01)
                                if remaining is None:
                                    break
                                if remaining.event_type in (
                                    VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
                                    VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
                                    VoicePipelineEventType.PIPELINE_ERROR.value,
                                    VoicePipelineEventType.LISTENING_STOPPED.value,
                                ):
                                    pipe_event = remaining
                                    break
                            if pipe_event is None:
                                logger.warning("VoicePipeline stopped unexpectedly without terminal event.")
                                self._publish_event(
                                    VoiceActivationEvent(
                                        event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                                        timestamp=time.time(),
                                        error="VoicePipeline stopped unexpectedly without transcription result.",
                                    )
                                )
                                break

                    if pipe_event is not None:
                        if pipe_event.event_type == VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value:
                            self._publish_event(
                                VoiceActivationEvent(
                                    event_type=VoiceActivationEventType.COMMAND_TRANSCRIBED.value,
                                    timestamp=time.time(),
                                    transcription=pipe_event.transcription,
                                    audio_duration_seconds=pipe_event.audio_duration_seconds,
                                )
                            )
                            pipeline_active = False
                        elif pipe_event.event_type == VoicePipelineEventType.TRANSCRIPTION_FAILED.value:
                            self._publish_event(
                                VoiceActivationEvent(
                                    event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                                    timestamp=time.time(),
                                    transcription=pipe_event.transcription,
                                    error=pipe_event.error or (
                                        pipe_event.transcription.error if pipe_event.transcription else "Transcription failed"
                                    ),
                                    audio_duration_seconds=pipe_event.audio_duration_seconds,
                                )
                            )
                            pipeline_active = False
                        elif pipe_event.event_type == VoicePipelineEventType.PIPELINE_ERROR.value:
                            self._publish_event(
                                VoiceActivationEvent(
                                    event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                                    timestamp=time.time(),
                                    error=pipe_event.error or "Voice pipeline error occurred",
                                    audio_duration_seconds=pipe_event.audio_duration_seconds,
                                )
                            )
                            pipeline_active = False
                        elif pipe_event.event_type == VoicePipelineEventType.LISTENING_STOPPED.value:
                            logger.warning("VoicePipeline stopped unexpectedly during active command listening.")
                            self._publish_event(
                                VoiceActivationEvent(
                                    event_type=VoiceActivationEventType.COMMAND_FAILED.value,
                                    timestamp=time.time(),
                                    error="VoicePipeline stopped unexpectedly before transcription completed.",
                                )
                            )
                            pipeline_active = False

                # 9. Cleanly stop VoicePipeline and verify clean shutdown
                pipeline_stopped_cleanly = True
                try:
                    if self._is_pipeline_active():
                        self.voice_pipeline.stop()
                except Exception as stop_err:
                    pipeline_stopped_cleanly = False
                    self._pipeline_shutdown_failed = True
                    logger.error("VoicePipeline shutdown failure in worker: %s", stop_err)
                    self._publish_event(
                        VoiceActivationEvent(
                            event_type=VoiceActivationEventType.ACTIVATION_ERROR.value,
                            timestamp=time.time(),
                            error=f"VoicePipeline shutdown failure: {stop_err}",
                        )
                    )

                # Never resume standby if pipeline shutdown failed or worker is still alive
                if not pipeline_stopped_cleanly or self._is_pipeline_active():
                    self._pipeline_shutdown_failed = True
                    logger.critical(
                        "VoicePipeline failed to terminate cleanly; halting activation orchestrator to prevent competing microphone consumers."
                    )
                    try:
                        self.capture_service.stop()
                    except Exception as cap_err:
                        logger.error("Error stopping capture service after pipeline shutdown failure: %s", cap_err)
                    break

                # 10. Reset the wake-word detector for the next cycle
                self.wake_word_detector.reset()

                # If stop was requested while pipeline was running, exit loop
                if self._stop_event.is_set():
                    break

        finally:
            # Terminal shutdown path: ensure capture and pipeline are halted
            try:
                if self._is_pipeline_active():
                    self.voice_pipeline.stop()
            except Exception as e:
                self._pipeline_shutdown_failed = True
                logger.debug("Error in final pipeline stop: %s", e)

            try:
                if self.capture_service.is_running:
                    self.capture_service.stop()
            except Exception as e:
                logger.debug("Error in final capture stop: %s", e)

            with self._lifecycle_lock:
                if self._session_id == session_id:
                    self._running = False

            logger.debug("VoiceActivationOrchestrator worker exited (session=%d)", session_id)
