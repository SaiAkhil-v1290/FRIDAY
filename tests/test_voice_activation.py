"""
Unit and integration tests for JARVIS Voice Activation Orchestrator (Milestone 5.1.5).
Verifies wake-word coordination, frame consumer handoff, VoicePipeline integration,
event model, error handling, bounded queues, and thread safety.
"""

import queue
import threading
import time
from unittest.mock import MagicMock
import pytest

from voice.config import VoiceConfig
from voice.audio_io import AudioCaptureService, AudioDeviceError
from voice.wake_word import WakeWordDetector, WakeWordResult
from voice.stt import TranscriptionResult
from voice.pipeline import (
    VoicePipeline,
    VoicePipelineEvent,
    VoicePipelineEventType,
    VoicePipelineError,
)
from voice.activation import (
    VoiceActivationOrchestrator,
    VoiceActivationEvent,
    VoiceActivationEventType,
    VoiceActivationError,
    CRITICAL_ACTIVATION_EVENT_TYPES,
)


class MockAudioCaptureService:
    """Mock AudioCaptureService that tracks active readers and yields controllable frames."""

    def __init__(self, config: VoiceConfig):
        self.config = config
        self._frame_queue = queue.Queue()
        self.is_running = False
        self.start_called = 0
        self.stop_called = 0
        self.fail_on_start = False
        self.fail_on_read = False
        self.read_failures_remaining: int = 0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.active_readers = 0
        self.max_concurrent_readers = 0

    def start(self) -> None:
        if self.fail_on_start:
            raise AudioDeviceError("Simulated microphone startup error")
        self.is_running = True
        self.start_called += 1
        self._stop_event.clear()

    def stop(self) -> None:
        self.is_running = False
        self.stop_called += 1
        self._stop_event.set()

    def clear_buffer(self) -> None:
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def feed_frame(self, frame: bytes) -> None:
        self._frame_queue.put(frame)

    def read_frame(self, timeout: float = 0.1) -> bytes | None:
        with self._lock:
            self.active_readers += 1
            if self.active_readers > self.max_concurrent_readers:
                self.max_concurrent_readers = self.active_readers

        try:
            if self.fail_on_read or self.read_failures_remaining > 0:
                if self.read_failures_remaining > 0:
                    self.read_failures_remaining -= 1
                raise AudioDeviceError("Simulated audio capture read error")
            try:
                return self._frame_queue.get(block=True, timeout=timeout)
            except queue.Empty:
                return None
        finally:
            with self._lock:
                self.active_readers -= 1


class MockWakeWordDetector:
    """Mock WakeWordDetector with controllable detection status."""

    def __init__(self, config: VoiceConfig, available: bool = True):
        self.config = config
        self._available = available
        self.initialization_error = None if available else "Simulated engine not installed"
        self.reset_called = 0
        self.stop_called = 0
        self.fail_on_process = False
        self.frames_to_detect: int = 0
        self._frames_seen: int = 0
        self.confidence_to_return: float = 0.85
        self.is_active = True

    def is_available(self) -> bool:
        return self._available and self.is_active

    def reset(self) -> None:
        self.reset_called += 1

    def stop(self) -> None:
        self.stop_called += 1
        self.is_active = False

    def trigger_on_next_frame(self, confidence: float = 0.90) -> None:
        self.frames_to_detect = 1
        self._frames_seen = 0
        self.confidence_to_return = confidence

    def process_frame(self, frame: bytes) -> WakeWordResult:
        if self.fail_on_process:
            raise RuntimeError("Simulated internal wake-word algorithm failure")

        self._frames_seen += 1
        if self.frames_to_detect > 0 and self._frames_seen >= self.frames_to_detect:
            self.frames_to_detect = 0
            return WakeWordResult(
                detected=True,
                wake_phrase=self.config.wake_word_phrase,
                confidence=self.confidence_to_return,
                timestamp=time.time(),
            )
        return WakeWordResult(
            detected=False,
            wake_phrase=self.config.wake_word_phrase,
            confidence=0.0,
            timestamp=time.time(),
        )


class MockVoicePipeline:
    """Mock VoicePipeline that coordinates with capture service and emits controllable events."""

    def __init__(self, config: VoiceConfig, capture_service: AudioCaptureService):
        self.config = config
        self.capture_service = capture_service
        self.is_running = False
        self.start_called = 0
        self.stop_called = 0
        self.fail_on_start = False
        self.fail_on_stop = False
        self.fail_on_clear_events = False
        self.fail_on_clear_events_after_start = False
        self.last_clear_capture_buffer = None
        self.clear_events_called = 0
        self.event_queue: queue.Queue[VoicePipelineEvent] = queue.Queue()
        self.on_start_callback = None
        self._worker_thread = None
        self._custom_worker_alive = None
        self._lock = threading.Lock()

    @property
    def worker_alive(self) -> bool:
        if self._custom_worker_alive is not None:
            return bool(self._custom_worker_alive)
        if self._worker_thread is not None and hasattr(self._worker_thread, "is_alive"):
            return bool(self._worker_thread.is_alive())
        return bool(self.is_running)

    def start(self, clear_capture_buffer: bool = True) -> None:
        self.last_clear_capture_buffer = clear_capture_buffer
        if self.fail_on_start:
            raise VoicePipelineError("Simulated pipeline startup error")
        with self._lock:
            self.is_running = True
            self.start_called += 1
        if self.on_start_callback:
            self.on_start_callback()

    def stop(self) -> None:
        if self.fail_on_stop:
            raise VoicePipelineError("Simulated pipeline shutdown error")
        with self._lock:
            self.is_running = False
            self.stop_called += 1
        # Replicate real VoicePipeline behavior of publishing LISTENING_STOPPED on stop
        self.event_queue.put(
            VoicePipelineEvent(
                event_type=VoicePipelineEventType.LISTENING_STOPPED.value,
                timestamp=time.time(),
            )
        )

    def queue_event(self, event: VoicePipelineEvent) -> None:
        self.event_queue.put(event)

    def clear_events(self) -> int:
        self.clear_events_called += 1
        if self.fail_on_clear_events:
            raise VoicePipelineError("Simulated pipeline clear_events error")
        if self.fail_on_clear_events_after_start and self.clear_events_called > 1:
            raise VoicePipelineError("Simulated pipeline per-cycle clear_events error")
        cleared = 0
        while not self.event_queue.empty():
            try:
                self.event_queue.get_nowait()
                cleared += 1
            except queue.Empty:
                break
        return cleared

    def read_event(self, timeout: float = 0.1) -> VoicePipelineEvent | None:
        try:
            return self.event_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None


def wait_for_event(
    orchestrator: VoiceActivationOrchestrator,
    event_type: str,
    timeout: float = 2.0,
) -> VoiceActivationEvent:
    """Helper to poll orchestrator until an event of given type arrives or timeout."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        ev = orchestrator.read_event(timeout=0.05)
        if ev is not None and ev.event_type == event_type:
            return ev
    raise TimeoutError(f"Timed out waiting for event '{event_type}' after {timeout}s")


@pytest.fixture
def base_config() -> VoiceConfig:
    return VoiceConfig(
        sample_rate=16000,
        frame_duration_ms=20,
        wake_word_phrase="Hey Friday",
        activation_event_queue_size=50,
        activation_frame_read_timeout_seconds=0.05,
        activation_pipeline_event_timeout_seconds=0.05,
        activation_join_timeout_seconds=1.0,
    )


# ==============================================================================
# 1. Startup and Availability Scenarios
# ==============================================================================

def test_successful_orchestrator_startup(base_config):
    """Verify that orchestrator starts background worker and publishes LISTENING_STARTED."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    orchestrator.start()
    assert orchestrator.is_running is True
    assert capture.is_running is True

    ev = wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)
    assert ev.event_type == VoiceActivationEventType.LISTENING_STARTED.value
    assert ev.timestamp > 0.0

    orchestrator.stop()
    assert orchestrator.is_running is False


def test_startup_rejected_when_wake_word_detector_unavailable(base_config):
    """Verify start() fails with VoiceActivationError and publishes ACTIVATION_ERROR when detector unavailable."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=False)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    with pytest.raises(VoiceActivationError, match="Wake-word detector is unavailable"):
        orchestrator.start()

    assert orchestrator.is_running is False
    assert capture.is_running is False

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "Wake-word detector is unavailable" in ev.error


# ==============================================================================
# 2. Standby and Wake-Word Activation Cycle
# ==============================================================================

def test_wake_word_standby_without_stt_calls(base_config):
    """Verify that during standby frames are processed without starting pipeline or calling Whisper."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    frame = b"\x00\x00" * base_config.frame_samples
    for _ in range(5):
        capture.feed_frame(frame)

    time.sleep(0.1)
    assert pipeline.start_called == 0
    assert pipeline.is_running is False

    orchestrator.stop()


def test_wake_word_detection_activates_voice_pipeline(base_config):
    """Verify that wake word detection triggers WAKE_WORD_DETECTED and starts VoicePipeline."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.92)
    frame = b"\x00\x00" * base_config.frame_samples
    capture.feed_frame(frame)

    ev_ww = wait_for_event(orchestrator, VoiceActivationEventType.WAKE_WORD_DETECTED.value)
    assert ev_ww.wake_word_result is not None
    assert ev_ww.wake_word_result.confidence == 0.92

    ev_cmd = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)
    assert ev_cmd.event_type == VoiceActivationEventType.COMMAND_LISTENING_STARTED.value

    # Pipeline should have been started
    time.sleep(0.05)
    assert pipeline.start_called >= 1
    assert pipeline.is_running is True

    orchestrator.stop()


def test_transcription_completion_publishes_command_event(base_config):
    """Verify that pipeline TRANSCRIPTION_COMPLETED produces COMMAND_TRANSCRIBED with text."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Simulate pipeline emitting transcription completed
    t_res = TranscriptionResult(text="open the calculator", success=True)
    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=t_res,
            audio_duration_seconds=1.8,
        )
    )

    ev_transcribed = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)
    assert ev_transcribed.transcription is not None
    assert ev_transcribed.transcription.text == "open the calculator"
    assert ev_transcribed.audio_duration_seconds == 1.8

    orchestrator.stop()


def test_transcription_failure_publishes_command_failed(base_config):
    """Verify that pipeline TRANSCRIPTION_FAILED produces COMMAND_FAILED with error."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.85)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
            timestamp=time.time(),
            error="Whisper transcription timeout",
            audio_duration_seconds=2.0,
        )
    )

    ev_failed = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_FAILED.value)
    assert "Whisper transcription timeout" in ev_failed.error
    assert ev_failed.audio_duration_seconds == 2.0

    orchestrator.stop()


# ==============================================================================
# 3. Resumption and Multi-Cycle Interaction
# ==============================================================================

def test_listening_resumes_after_successful_transcription(base_config):
    """Verify orchestrator returns to wake-word standby after transcription completes."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    # Cycle 1
    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="first command", success=True),
        )
    )
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)

    # Wait for orchestrator to stop pipeline and reset detector
    time.sleep(0.1)
    assert detector.reset_called >= 1
    assert pipeline.is_running is False
    assert capture.is_running is True

    # Cycle 2: another wake word triggers
    detector.trigger_on_next_frame(confidence=0.91)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="second command", success=True),
        )
    )
    ev2 = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)
    assert ev2.transcription.text == "second command"

    orchestrator.stop()


def test_listening_resumes_after_transcription_failure(base_config):
    """Verify orchestrator resumes standby listening after a failed command."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    # Cycle 1: failed command
    detector.trigger_on_next_frame(confidence=0.80)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value,
            timestamp=time.time(),
            error="Transcription failed",
        )
    )
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_FAILED.value)

    time.sleep(0.1)
    assert detector.reset_called >= 1

    # Cycle 2: successful command
    detector.trigger_on_next_frame(confidence=0.95)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="recovered command", success=True),
        )
    )
    ev2 = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)
    assert ev2.transcription.text == "recovered command"

    orchestrator.stop()


# ==============================================================================
# 4. Component Wiring and Single-Consumer Guarantee
# ==============================================================================

def test_same_audio_capture_service_is_shared(base_config):
    """Verify that orchestrator and voice_pipeline strictly share the same capture service."""
    capture = MockAudioCaptureService(base_config)
    pipeline = MockVoicePipeline(base_config, capture)
    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        voice_pipeline=pipeline,
    )
    assert orchestrator.capture_service is capture
    assert orchestrator.voice_pipeline.capture_service is capture


def test_incompatible_capture_service_wiring_rejected(base_config):
    """Verify that injecting mismatched capture services raises VoiceActivationError."""
    capture1 = MockAudioCaptureService(base_config)
    capture2 = MockAudioCaptureService(base_config)
    pipeline = MockVoicePipeline(base_config, capture1)

    with pytest.raises(VoiceActivationError, match="Incompatible component wiring"):
        VoiceActivationOrchestrator(
            config=base_config,
            capture_service=capture2,
            voice_pipeline=pipeline,
        )


def test_no_concurrent_microphone_consumers(base_config):
    """Verify that detector and pipeline never read from capture service concurrently."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    # In pipeline start, simulate pipeline reading a frame from the shared capture
    def pipeline_read_sim():
        capture.read_frame(timeout=0.01)

    pipeline.on_start_callback = pipeline_read_sim

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.89)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)
    time.sleep(0.05)

    assert capture.max_concurrent_readers <= 1

    orchestrator.stop()


# ==============================================================================
# 5. Idempotence and Lifecycle Concurrency
# ==============================================================================

def test_start_is_idempotent(base_config):
    """Verify that calling start() multiple times does not launch duplicate workers."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    orchestrator.start()
    worker_1 = orchestrator._worker_thread
    assert orchestrator.is_running is True

    orchestrator.start()
    assert orchestrator._worker_thread is worker_1

    orchestrator.stop()


def test_stop_is_idempotent(base_config):
    """Verify that calling stop() multiple times is safe and idempotent."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    orchestrator.start()
    orchestrator.stop()
    assert orchestrator.is_running is False

    # Second call should not raise
    orchestrator.stop()
    assert orchestrator.is_running is False


def test_stop_while_waiting_for_audio_frames(base_config):
    """Verify clean shutdown when orchestrator is blocked waiting for audio frames in standby."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()
    time.sleep(0.05)

    start_stop = time.time()
    orchestrator.stop()
    stop_duration = time.time() - start_stop

    assert orchestrator.is_running is False
    assert stop_duration < 1.0


def test_stop_while_voice_pipeline_is_processing(base_config):
    """Verify that stop() halts active VoicePipeline and joins orchestrator worker cleanly."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    assert pipeline.is_running is True

    orchestrator.stop()
    assert orchestrator.is_running is False
    assert pipeline.is_running is False
    assert capture.is_running is False


def test_stop_during_transcription_operation(base_config):
    """Verify stop() handles in-progress transcription without deadlock."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.85)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Call stop before pipeline emits any transcription event
    orchestrator.stop()
    assert orchestrator.is_running is False


# ==============================================================================
# 6. Error Handling and Resilience
# ==============================================================================

def test_capture_startup_failure(base_config):
    """Verify start() raises VoiceActivationError if capture_service fails to start."""
    capture = MockAudioCaptureService(base_config)
    capture.fail_on_start = True
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    with pytest.raises(VoiceActivationError, match="Capture service startup failed"):
        orchestrator.start()

    assert orchestrator.is_running is False
    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "Capture service startup failed" in ev.error


def test_capture_read_failure(base_config):
    """Verify that audio read errors in worker emit ACTIVATION_ERROR and terminate cleanly."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    capture.fail_on_read = True
    time.sleep(0.1)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "Capture read failure" in ev.error

    orchestrator.stop()


def test_wake_word_detector_exception(base_config):
    """Verify that detector algorithm exceptions in worker emit ACTIVATION_ERROR and exit safely."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.fail_on_process = True
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "Wake-word detector exception" in ev.error

    orchestrator.stop()


def test_voice_pipeline_startup_failure(base_config):
    """Verify that failure to start pipeline emits COMMAND_FAILED and resumes standby."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)
    pipeline.fail_on_start = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_FAILED.value)
    assert "VoicePipeline startup failure" in ev.error

    time.sleep(0.1)
    assert detector.reset_called >= 1
    # Verify cleanup was attempted on pipeline
    assert pipeline.stop_called >= 1
    # Verify standby resumed
    assert orchestrator.is_running is True
    assert capture.is_running is True

    orchestrator.stop()


def test_voice_pipeline_startup_raises_and_cleanup_raises_fails_closed(base_config):
    """
    Verify that when pipeline.start() raises AND cleanup stop() raises:
    - ACTIVATION_ERROR is published
    - Capture service is stopped
    - Orchestrator worker terminates and never resumes standby
    - Subsequent orchestrator.stop() raises VoiceActivationError
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)
    pipeline.fail_on_start = True
    pipeline.fail_on_stop = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.91)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "VoicePipeline startup failure" in ev.error

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not orchestrator.is_running and not capture.is_running:
            break
        time.sleep(0.02)

    assert orchestrator.is_running is False
    assert capture.is_running is False

    with pytest.raises(VoiceActivationError, match="VoicePipeline failed to terminate cleanly"):
        orchestrator.stop()


def test_voice_pipeline_startup_partially_initializes_worker_fails_closed(base_config):
    """
    Verify that if pipeline.start() partially initializes an unkillable worker before raising:
    - The orchestrator detects the pipeline worker is still alive
    - Fails closed: publishes ACTIVATION_ERROR, halts worker, stops capture
    - Calling stop() raises VoiceActivationError
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)

    class PartialInitPipeline(MockVoicePipeline):
        def __init__(self, cfg, cap):
            super().__init__(cfg, cap)
            self._orphan_worker = None
            self._keep_running = True

        @property
        def worker_alive(self) -> bool:
            return self._orphan_worker is not None and self._orphan_worker.is_alive()

        def start(self, clear_capture_buffer: bool = True):
            self.last_clear_capture_buffer = clear_capture_buffer
            self._orphan_worker = threading.Thread(target=self._worker_run, daemon=True)
            self._orphan_worker.start()
            self._worker_thread = self._orphan_worker
            self.is_running = False
            raise VoicePipelineError("Partial startup initialization error")

        def _worker_run(self):
            while self._keep_running:
                time.sleep(0.01)

        def stop(self):
            self.stop_called += 1

    pipeline = PartialInitPipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.89)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    try:
        ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
        assert "VoicePipeline startup failure" in ev.error
        assert "cleanup failed to terminate cleanly" in ev.error

        # Verify that worker_alive was True while is_running was False
        assert pipeline.worker_alive is True
        assert pipeline.is_running is False
        assert orchestrator._is_pipeline_active() is True

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not orchestrator.is_running and not capture.is_running:
                break
            time.sleep(0.02)

        assert orchestrator.is_running is False
        assert capture.is_running is False

        with pytest.raises(VoiceActivationError, match="VoicePipeline failed to terminate cleanly"):
            orchestrator.stop()
    finally:
        pipeline._keep_running = False
        if pipeline._orphan_worker:
            pipeline._orphan_worker.join(timeout=1.0)


def test_lifecycle_status_query_failure_fails_closed(base_config):
    """
    Verify that if querying either required lifecycle property (is_running or worker_alive)
    raises an exception, _is_pipeline_active() treats the pipeline as active and fails closed.
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)

    class BrokenLifecyclePipeline(MockVoicePipeline):
        def __init__(self, cfg, cap):
            super().__init__(cfg, cap)
            self.raise_on_worker_alive = True

        @property
        def worker_alive(self) -> bool:
            if self.raise_on_worker_alive:
                raise RuntimeError("Hardware bus error reading worker thread state")
            return False

    pipeline = BrokenLifecyclePipeline(base_config, capture)
    pipeline.fail_on_start = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    # 1. Directly verify _is_pipeline_active fails closed when worker_alive raises
    assert orchestrator._is_pipeline_active() is True

    # 2. Verify when is_running property raises, it also fails closed
    class BrokenRunningPipeline(MockVoicePipeline):
        def __init__(self, cfg, cap):
            super().__init__(cfg, cap)
            self._raise_on_running = True

        @property
        def is_running(self) -> bool:
            if getattr(self, "_raise_on_running", False):
                raise RuntimeError("Cannot evaluate is_running")
            return False

        @is_running.setter
        def is_running(self, val):
            pass

    broken_running = BrokenRunningPipeline(base_config, capture)
    orchestrator_broken_running = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=broken_running,
    )
    assert orchestrator_broken_running._is_pipeline_active() is True

    # 3. Integration verification: orchestrator fails closed when lifecycle status fails during startup cleanup
    orchestrator.start()
    detector.trigger_on_next_frame(confidence=0.90)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "cleanup failed to terminate cleanly" in ev.error

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not orchestrator.is_running and not capture.is_running:
            break
        time.sleep(0.02)

    assert orchestrator.is_running is False
    assert capture.is_running is False

    with pytest.raises(VoiceActivationError, match="VoicePipeline failed to terminate cleanly"):
        orchestrator.stop()


def test_initial_clear_events_failure_fails_startup_cleanly(base_config):
    """
    Verify that if pipeline.clear_events() fails during orchestrator.start():
    - start() raises VoiceActivationError cleanly
    - Capture service is NOT started / left running
    - Orchestrator worker is not running
    - ACTIVATION_ERROR is published
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)
    pipeline.fail_on_clear_events = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    with pytest.raises(VoiceActivationError, match="Initial pipeline event drainage failed"):
        orchestrator.start()

    assert orchestrator.is_running is False
    assert capture.is_running is False
    assert capture.start_called == 0

    ev = orchestrator.read_event(timeout=0.1)
    assert ev is not None
    assert ev.event_type == VoiceActivationEventType.ACTIVATION_ERROR.value
    assert "Initial pipeline event drainage failed" in ev.error


def test_per_cycle_clear_events_failure_fails_closed_and_stops_capture(base_config):
    """
    Verify that if pipeline.clear_events() fails after wake-word detection:
    - Pipeline is NOT started
    - ACTIVATION_ERROR is published
    - Capture service is stopped
    - Orchestrator worker halts and does NOT resume standby
    - Calling stop() reports VoiceActivationError
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)
    pipeline.fail_on_clear_events_after_start = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()
    wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)

    detector.trigger_on_next_frame(confidence=0.92)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "Per-cycle pipeline event drainage failed" in ev.error

    # Pipeline must NOT have been started
    assert pipeline.start_called == 0

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not orchestrator.is_running and not capture.is_running:
            break
        time.sleep(0.02)

    assert orchestrator.is_running is False
    assert capture.is_running is False

    with pytest.raises(VoiceActivationError, match="VoicePipeline failed to terminate cleanly"):
        orchestrator.stop()


def test_voice_pipeline_shutdown_failure(base_config):
    """
    Verify that failure to stop pipeline emits ACTIVATION_ERROR, halts the orchestrator worker,
    stops AudioCaptureService, does not resume wake-word standby, and calling stop() fails clearly.
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Set fail_on_stop before transcription completes
    pipeline.fail_on_stop = True
    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="testing shutdown failure", success=True),
        )
    )

    ev = wait_for_event(orchestrator, VoiceActivationEventType.ACTIVATION_ERROR.value)
    assert "VoicePipeline shutdown failure" in ev.error

    # Give worker a moment to terminate
    time.sleep(0.1)

    # 1. Standby must NOT resume
    assert orchestrator.is_running is False
    # 2. Capture must be stopped
    assert capture.is_running is False
    # 3. Calling stop() must raise VoiceActivationError because pipeline failed to terminate cleanly
    with pytest.raises(VoiceActivationError, match="VoicePipeline failed to terminate cleanly"):
        orchestrator.stop()


def test_preserve_audio_captured_after_wake_word_and_fifo_order(base_config):
    """
    Verify that frames queued after wake-word detection are preserved
    when VoicePipeline starts (clear_capture_buffer=False) and received in FIFO order.
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    received_frames = []
    pipeline_started_event = threading.Event()

    def on_pipeline_start():
        pipeline_started_event.set()
        while True:
            f = capture.read_frame(timeout=0.01)
            if f is None:
                break
            received_frames.append(f)

    pipeline.on_start_callback = on_pipeline_start

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    # 1. Trigger wake-word on next frame
    detector.trigger_on_next_frame(confidence=0.90)

    # Feed wake frame
    wake_frame = b"\xaa\xbb" * base_config.frame_samples
    capture.feed_frame(wake_frame)

    # 2. Feed 3 post-wake speech frames before pipeline consumes them
    frame1 = b"\x01\x01" * base_config.frame_samples
    frame2 = b"\x02\x02" * base_config.frame_samples
    frame3 = b"\x03\x03" * base_config.frame_samples
    capture.feed_frame(frame1)
    capture.feed_frame(frame2)
    capture.feed_frame(frame3)

    # Wait for pipeline to start
    assert pipeline_started_event.wait(timeout=2.0) is True

    # 3. Verify pipeline was started with clear_capture_buffer=False
    assert pipeline.last_clear_capture_buffer is False

    # 4. Verify all post-wake frames reached the pipeline in exact FIFO order
    assert len(received_frames) == 3
    assert received_frames[0] == frame1
    assert received_frames[1] == frame2
    assert received_frames[2] == frame3

    orchestrator.stop()


def test_startup_event_ordering_on_success(base_config):
    """
    Verify event ordering on wake-word detection:
    1. WAKE_WORD_DETECTED
    2. COMMAND_LISTENING_STARTED (strictly after pipeline start)
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()
    wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev1 = wait_for_event(orchestrator, VoiceActivationEventType.WAKE_WORD_DETECTED.value)
    ev2 = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    assert ev1.event_type == VoiceActivationEventType.WAKE_WORD_DETECTED.value
    assert ev2.event_type == VoiceActivationEventType.COMMAND_LISTENING_STARTED.value
    assert pipeline.start_called >= 1

    orchestrator.stop()


def test_startup_event_ordering_on_pipeline_failure(base_config):
    """
    Verify that if pipeline.start() fails:
    1. WAKE_WORD_DETECTED is emitted
    2. COMMAND_FAILED is emitted
    3. COMMAND_LISTENING_STARTED is NEVER emitted
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)
    pipeline.fail_on_start = True

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()
    wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)

    ev_ww = wait_for_event(orchestrator, VoiceActivationEventType.WAKE_WORD_DETECTED.value)
    assert ev_ww.event_type == VoiceActivationEventType.WAKE_WORD_DETECTED.value

    ev_failed = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_FAILED.value)
    assert "VoicePipeline startup failure" in ev_failed.error

    # Ensure COMMAND_LISTENING_STARTED was never published
    events = []
    while True:
        e = orchestrator.read_event(timeout=0.01)
        if e is None:
            break
        events.append(e)

    types = [e.event_type for e in events]
    assert VoiceActivationEventType.COMMAND_LISTENING_STARTED.value not in types

    orchestrator.stop()


def test_unexpected_listening_stopped_during_command_listening(base_config):
    """
    Verify that receiving LISTENING_STOPPED from pipeline during active command listening
    is handled as an abnormal termination (COMMAND_FAILED), not as success.
    """
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    detector.trigger_on_next_frame(confidence=0.88)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Inject unexpected LISTENING_STOPPED into pipeline
    pipeline.queue_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.LISTENING_STOPPED.value,
            timestamp=time.time(),
        )
    )

    ev_failed = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_FAILED.value)
    assert "VoicePipeline stopped unexpectedly" in ev_failed.error

    orchestrator.stop()


def test_real_voice_pipeline_multi_cycle_activation_clears_stale_events(base_config):
    """
    Integration test using the actual VoicePipeline class and event queue.
    Executes at least two complete activation cycles to prove:
      1. LISTENING_STOPPED published by pipeline.stop() in cycle 1 is drained
      2. Cycle 2 starts with a clean pipeline queue and does not abort prematurely
      3. Both cycles transcribe commands successfully
    """
    from voice.vad import VoiceActivityDetector

    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)

    fake_vad = MagicMock(spec=VoiceActivityDetector)
    fake_vad.reset = MagicMock()

    fake_stt = MagicMock()
    fake_stt.is_available.return_value = True

    # Real VoicePipeline instance
    pipeline = VoicePipeline(
        config=base_config,
        capture_service=capture,
        vad=fake_vad,
        stt_service=fake_stt,
    )

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()

    # === CYCLE 1 ===
    detector.trigger_on_next_frame(confidence=0.90)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Deliver transcription completion in real pipeline event queue
    pipeline._publish_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="cycle 1 command", success=True),
            audio_duration_seconds=1.5,
        )
    )

    ev1 = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)
    assert ev1.transcription.text == "cycle 1 command"

    # Allow worker to finish cycle 1 cleanup (which calls pipeline.stop(), enqueueing LISTENING_STOPPED in pipeline)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not pipeline.is_running and capture.is_running:
            break
        time.sleep(0.02)
    assert pipeline.is_running is False
    assert capture.is_running is True

    # === CYCLE 2 ===
    detector.trigger_on_next_frame(confidence=0.92)
    capture.feed_frame(b"\x00\x00" * base_config.frame_samples)
    wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

    # Deliver transcription completion for cycle 2
    pipeline._publish_event(
        VoicePipelineEvent(
            event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="cycle 2 command", success=True),
            audio_duration_seconds=2.0,
        )
    )

    ev2 = wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_TRANSCRIBED.value)
    assert ev2.transcription.text == "cycle 2 command"

    orchestrator.stop()


def test_real_voice_pipeline_fifo_handoff_and_frame_preservation(base_config):
    """
    Integration test using the actual VoicePipeline class and event loop.
    Verifies that:
      1. Wake frame triggers wake-word detection
      2. Distinct post-wake frames queued in capture buffer are preserved on handoff
      3. Actual VoicePipeline VAD receives every post-wake frame exactly once, in original FIFO order
      4. The wake frame itself is NOT reprocessed by the pipeline VAD
      5. Workers are cleanly stopped even if assertions fail
    """
    from voice.vad import VoiceActivityDetector

    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)

    class RecordingFakeVAD:
        def __init__(self):
            self.received_frames = []
            self.frame_received_event = threading.Event()
            self.expected_frames_count = 3
            self._lock = threading.Lock()

        def reset(self):
            pass

        def process_frame(self, frame: bytes):
            with self._lock:
                self.received_frames.append(frame)
                if len(self.received_frames) >= self.expected_frames_count:
                    self.frame_received_event.set()
            return []

    fake_vad = RecordingFakeVAD()
    fake_stt = MagicMock()
    fake_stt.is_available.return_value = True

    # Real VoicePipeline instance with actual processing worker thread
    pipeline = VoicePipeline(
        config=base_config,
        capture_service=capture,
        vad=fake_vad,
        stt_service=fake_stt,
    )

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    # Prepare distinct frames with valid lengths
    frame_len = base_config.frame_bytes
    wake_frame = b"\xaa" * frame_len
    cmd_frame_1 = b"\x11" * frame_len
    cmd_frame_2 = b"\x22" * frame_len
    cmd_frame_3 = b"\x33" * frame_len

    orchestrator.start()
    try:
        wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)

        # 1. Trigger wake word on the next frame
        detector.trigger_on_next_frame(confidence=0.94)

        # 2. Feed wake frame and immediately queue 3 distinct post-wake frames into capture
        capture.feed_frame(wake_frame)
        capture.feed_frame(cmd_frame_1)
        capture.feed_frame(cmd_frame_2)
        capture.feed_frame(cmd_frame_3)

        # 3. Confirm orchestrator entered command listening
        wait_for_event(orchestrator, VoiceActivationEventType.COMMAND_LISTENING_STARTED.value)

        # 4. Wait for real pipeline worker to read and deliver all 3 post-wake frames to VAD
        assert fake_vad.frame_received_event.wait(timeout=3.0) is True

        # 5. Verify post-wake frames were delivered exactly once and in strict FIFO order
        assert len(fake_vad.received_frames) == 3
        assert fake_vad.received_frames[0] == cmd_frame_1
        assert fake_vad.received_frames[1] == cmd_frame_2
        assert fake_vad.received_frames[2] == cmd_frame_3

        # 6. Verify wake frame was NOT processed as command audio
        assert wake_frame not in fake_vad.received_frames

    finally:
        # Guarantee clean shutdown of both workers regardless of assertion outcome
        try:
            orchestrator.stop()
        except Exception:
            pass
        try:
            if pipeline.is_running:
                pipeline.stop()
        except Exception:
            pass


# ==============================================================================
# 7. Queue Overflow and Critical Event Preservation
# ==============================================================================

def test_bounded_event_queue_behavior(base_config):
    """Verify event queue does not grow beyond activation_event_queue_size."""
    cfg = VoiceConfig(
        sample_rate=16000,
        wake_word_phrase="Hey Friday",
        activation_event_queue_size=5,
    )
    capture = MockAudioCaptureService(cfg)
    detector = MockWakeWordDetector(cfg, available=True)
    pipeline = MockVoicePipeline(cfg, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=cfg,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    # Publish 10 ordinary events
    for i in range(10):
        orchestrator._publish_event(
            VoiceActivationEvent(
                event_type=VoiceActivationEventType.WAKE_WORD_DETECTED.value,
                timestamp=time.time(),
            )
        )

    assert orchestrator._event_queue.qsize() == 5


def test_critical_event_preservation(base_config):
    """Verify that critical events evict ordinary events when queue is full."""
    cfg = VoiceConfig(
        sample_rate=16000,
        wake_word_phrase="Hey Friday",
        activation_event_queue_size=3,
    )
    capture = MockAudioCaptureService(cfg)
    detector = MockWakeWordDetector(cfg, available=True)
    pipeline = MockVoicePipeline(cfg, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=cfg,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    # Publish 3 ordinary events
    for _ in range(3):
        orchestrator._publish_event(
            VoiceActivationEvent(
                event_type=VoiceActivationEventType.WAKE_WORD_DETECTED.value,
                timestamp=time.time(),
            )
        )

    # Now publish critical event
    orchestrator._publish_event(
        VoiceActivationEvent(
            event_type=VoiceActivationEventType.COMMAND_TRANSCRIBED.value,
            timestamp=time.time(),
            transcription=TranscriptionResult(text="critical command", success=True),
        )
    )

    assert orchestrator._event_queue.qsize() == 3

    # Drain events and verify critical event is present
    events = []
    while True:
        e = orchestrator.read_event(timeout=0.01)
        if e is None:
            break
        events.append(e)

    types = [e.event_type for e in events]
    assert VoiceActivationEventType.COMMAND_TRANSCRIBED.value in types


# ==============================================================================
# 8. Worker Termination and Stale Session Isolation
# ==============================================================================

def test_restart_after_clean_shutdown(base_config):
    """Verify orchestrator can be cleanly restarted multiple times."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    for cycle in range(3):
        orchestrator.start()
        assert orchestrator.is_running is True
        ev = wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STARTED.value)
        assert ev.event_type == VoiceActivationEventType.LISTENING_STARTED.value

        orchestrator.stop()
        assert orchestrator.is_running is False
        ev_stop = wait_for_event(orchestrator, VoiceActivationEventType.LISTENING_STOPPED.value)
        assert ev_stop.event_type == VoiceActivationEventType.LISTENING_STOPPED.value


def test_no_stale_worker_interference_after_restart(base_config):
    """Verify that a worker from a previous session cannot modify newer session state."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )

    orchestrator.start()
    session_1 = orchestrator._session_id

    orchestrator.stop()

    # Start session 2
    orchestrator.start()
    session_2 = orchestrator._session_id
    assert session_2 > session_1

    # If worker 1 were to run with session_1, it should immediately exit without altering running state
    orchestrator._orchestrator_worker(session_id=session_1)

    assert orchestrator.is_running is True
    assert orchestrator._session_id == session_2

    orchestrator.stop()


def test_worker_thread_termination_and_resource_cleanup(base_config):
    """Verify that worker thread terminates and resources are released on stop()."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    orchestrator.start()
    worker = orchestrator._worker_thread

    orchestrator.stop()
    assert worker.is_alive() is False
    assert orchestrator._worker_thread is None
    assert capture.is_running is False


def test_clear_events_returns_exact_count(base_config):
    """Verify clear_events() empties the event queue and returns the exact number of events cleared."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    assert orchestrator.clear_events() == 0

    for _ in range(7):
        orchestrator._publish_event(
            VoiceActivationEvent(
                event_type=VoiceActivationEventType.WAKE_WORD_DETECTED.value,
                timestamp=time.time(),
            )
        )
    assert orchestrator.clear_events() == 7
    assert orchestrator.clear_events() == 0


def test_read_event_empty_returns_none(base_config):
    """Verify read_event() returns None on empty queue with timeout."""
    capture = MockAudioCaptureService(base_config)
    detector = MockWakeWordDetector(base_config, available=True)
    pipeline = MockVoicePipeline(base_config, capture)

    orchestrator = VoiceActivationOrchestrator(
        config=base_config,
        capture_service=capture,
        wake_word_detector=detector,
        voice_pipeline=pipeline,
    )
    assert orchestrator.read_event(timeout=0.01) is None
    assert orchestrator.read_event(timeout=None) is None

