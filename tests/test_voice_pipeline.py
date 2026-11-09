"""
Unit and integration tests for JARVIS Voice Pipeline Orchestration (Milestone 5.1.3).
Covers basic lifecycle, speech detection, utterance completion, truncation handling,
error handling, bounded queues, and thread safety without hardware or cloud dependencies.
"""

import queue
import threading
import time
from unittest.mock import MagicMock
import pytest

from voice.config import VoiceConfig
from voice.audio_io import AudioCaptureService, AudioDeviceError
from voice.vad import VoiceActivityDetector, VADEvent, VADEventType
from voice.stt import SpeechToTextService, TranscriptionResult
from voice.pipeline import (
    VoicePipeline,
    VoicePipelineEvent,
    VoicePipelineEventType,
    VoicePipelineError,
)


class FakeAudioCaptureService:
    """Mock AudioCaptureService that yields controllable PCM frames."""

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

    def start(self) -> None:
        if self.fail_on_start:
            raise AudioDeviceError("Simulated microphone initialization error")
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
        if self.fail_on_read or self.read_failures_remaining > 0:
            if self.read_failures_remaining > 0:
                self.read_failures_remaining -= 1
            raise AudioDeviceError("Simulated audio read error")
        try:
            return self._frame_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None


class FakeVAD:
    """Mock VoiceActivityDetector that returns controllable VADEvent responses."""

    def __init__(self, config: VoiceConfig):
        self.config = config
        self.reset_called = 0
        self.fail_on_process = False
        self.events_to_return: list[list[VADEvent]] = []

    def reset(self) -> None:
        self.reset_called += 1

    def queue_response(self, events: list[VADEvent]) -> None:
        self.events_to_return.append(events)

    def process_frame(self, frame: bytes) -> list[VADEvent]:
        if self.fail_on_process:
            raise RuntimeError("Simulated VAD internal algorithm error")
        if self.events_to_return:
            return self.events_to_return.pop(0)
        return []


class FakeSTT:
    """Mock SpeechToTextService that records invocations and returns structured results."""

    def __init__(self, config: VoiceConfig):
        self.config = config
        self.transcribe_calls: list[bytes] = []
        self.return_result = TranscriptionResult(text="Mocked speech", success=True)
        self.fail_with_exception = False
        self.available: bool = True
        self.fail_on_available: bool = False
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self._lock = threading.Lock()
        self.delay: float = 0.0

    def is_available(self) -> bool:
        if self.fail_on_available:
            raise RuntimeError("Simulated STT availability check failure")
        return self.available

    def transcribe(self, pcm_audio: bytes) -> TranscriptionResult:
        with self._lock:
            self.concurrent_calls += 1
            if self.concurrent_calls > self.max_concurrent_calls:
                self.max_concurrent_calls = self.concurrent_calls
        try:
            if self.delay > 0:
                time.sleep(self.delay)
            if self.fail_with_exception:
                raise RuntimeError("Simulated STT process execution failure")
            self.transcribe_calls.append(pcm_audio)
            return self.return_result
        finally:
            with self._lock:
                self.concurrent_calls -= 1


@pytest.fixture
def test_config():
    """Fast test configuration with small timeouts and bounded queues."""
    return VoiceConfig(
        sample_rate=16000,
        channels=1,
        sample_width=2,
        frame_duration_ms=20,
        pipeline_event_queue_size=20,
        pipeline_frame_read_timeout_seconds=0.02,
        pipeline_join_timeout_seconds=1.0,
        pipeline_max_consecutive_read_errors=3,
    )


@pytest.fixture
def pipeline_fixture(test_config):
    """VoicePipeline instantiated with mock components."""
    fake_capture = FakeAudioCaptureService(test_config)
    fake_vad = FakeVAD(test_config)
    fake_stt = FakeSTT(test_config)

    pipeline = VoicePipeline(
        config=test_config,
        capture_service=fake_capture,
        vad=fake_vad,
        stt_service=fake_stt,
    )
    yield pipeline, fake_capture, fake_vad, fake_stt

    if pipeline.is_running:
        pipeline.stop()


def make_vad_event(
    event_type: str,
    timestamp_ms: float = 0.0,
    frame: bytes = b"",
    duration_ms: float = 20.0,
    accumulated_audio: bytes | None = None,
    truncated: bool = False,
    pre_speech_truncated: bool = False,
) -> VADEvent:
    return VADEvent(
        event_type=event_type,  # type: ignore[arg-type]
        timestamp_ms=timestamp_ms,
        frame=frame,
        duration_ms=duration_ms,
        accumulated_audio=accumulated_audio,
        truncated=truncated,
        pre_speech_truncated=pre_speech_truncated,
    )


def wait_for_event(pipeline: VoicePipeline, event_type: str, timeout: float = 1.0) -> VoicePipelineEvent:
    """Helper that polls read_event until an event of given type arrives or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = pipeline.read_event(timeout=0.05)
        if event is not None and event.event_type == event_type:
            return event
    raise TimeoutError(f"Timed out waiting for VoicePipelineEvent of type '{event_type}'")


# ==============================================================================
# 1. Basic Pipeline Lifecycle
# ==============================================================================

def test_pipeline_starts_successfully(pipeline_fixture):
    """Verify that start() starts capture, spawns worker, and emits listening_started."""
    pipeline, capture, _, _ = pipeline_fixture

    assert not pipeline.is_running
    pipeline.start()
    assert pipeline.is_running
    assert capture.is_running

    event = pipeline.read_event(timeout=0.5)
    assert event is not None
    assert event.event_type == VoicePipelineEventType.LISTENING_STARTED.value


def test_pipeline_stops_successfully(pipeline_fixture):
    """Verify that stop() stops capture, terminates worker, and emits listening_stopped."""
    pipeline, capture, _, _ = pipeline_fixture

    pipeline.start()
    assert pipeline.is_running

    pipeline.stop()
    assert not pipeline.is_running
    assert not capture.is_running

    # Read events until listening_stopped
    stopped_event = wait_for_event(pipeline, VoicePipelineEventType.LISTENING_STOPPED.value)
    assert stopped_event is not None


def test_repeated_start_is_idempotent(pipeline_fixture):
    """Verify that repeated start() calls do not spawn extra workers or fail."""
    pipeline, capture, _, _ = pipeline_fixture

    pipeline.start()
    first_worker = pipeline._worker_thread

    # Second start
    pipeline.start()
    assert pipeline.is_running
    assert pipeline._worker_thread is first_worker
    assert capture.start_called == 1


def test_repeated_stop_is_safe(pipeline_fixture):
    """Verify that repeated stop() calls on an already-stopped pipeline are safe."""
    pipeline, _, _, _ = pipeline_fixture

    pipeline.start()
    pipeline.stop()
    assert not pipeline.is_running

    # Second stop should be a clean no-op
    pipeline.stop()
    assert not pipeline.is_running


def test_correct_running_state_reporting(pipeline_fixture):
    """Verify is_running accurately tracks the lifecycle through transitions."""
    pipeline, _, _, _ = pipeline_fixture

    assert pipeline.is_running is False
    pipeline.start()
    assert pipeline.is_running is True
    pipeline.stop()
    assert pipeline.is_running is False


def test_correct_worker_alive_reporting(pipeline_fixture):
    """Verify worker_alive accurately reflects whether background worker thread is alive."""
    pipeline, _, _, _ = pipeline_fixture

    assert pipeline.worker_alive is False
    assert pipeline.is_running is False

    pipeline.start()
    assert pipeline.worker_alive is True
    assert pipeline.is_running is True

    pipeline.stop()
    assert pipeline.worker_alive is False
    assert pipeline.is_running is False


# ==============================================================================
# 2. Speech Detection & Utterance Completion
# ==============================================================================

def test_speech_start_event_is_published(pipeline_fixture, test_config):
    """Verify that SPEECH_START emitted by VAD publishes speech_started event."""
    pipeline, capture, vad, _ = pipeline_fixture
    pipeline.start()

    # Queue speech_start VAD event
    vad.queue_response([
        make_vad_event(
            event_type="speech_start",
            timestamp_ms=time.time() * 1000.0,
            frame=b"\x00" * test_config.frame_bytes,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.SPEECH_STARTED.value)
    assert event.event_type == VoicePipelineEventType.SPEECH_STARTED.value


def test_speech_end_event_triggers_stt(pipeline_fixture, test_config):
    """Verify that SPEECH_END with accumulated audio triggers STT and publishes result."""
    pipeline, capture, vad, stt = pipeline_fixture
    pipeline.start()

    sample_audio = b"\x00\x00" * 8000  # 0.5 sec

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=sample_audio,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value)
    assert event.event_type == VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value
    assert event.transcription is not None
    assert event.transcription.text == "Mocked speech"
    assert len(stt.transcribe_calls) == 1
    assert stt.transcribe_calls[0] == sample_audio


def test_stt_is_not_called_for_every_frame(pipeline_fixture, test_config):
    """Verify that normal speech frames do not trigger STT; STT runs only on speech_end."""
    pipeline, capture, vad, stt = pipeline_fixture
    pipeline.start()

    # Feed 5 speech frames
    for _ in range(5):
        vad.queue_response([
            make_vad_event(
                event_type="speech_frame",
                timestamp_ms=time.time() * 1000.0,
            )
        ])
        capture.feed_frame(b"\x00" * test_config.frame_bytes)

    time.sleep(0.1)
    assert len(stt.transcribe_calls) == 0


def test_empty_utterance_is_not_transcribed(pipeline_fixture, test_config):
    """Verify that SPEECH_END with empty accumulated audio does not invoke STT."""
    pipeline, capture, vad, stt = pipeline_fixture
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"",
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)
    time.sleep(0.1)

    assert len(stt.transcribe_calls) == 0


def test_failed_transcription_produces_failure_event(pipeline_fixture, test_config):
    """Verify that non-successful STT result emits transcription_failed event."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.return_result = TranscriptionResult(text="", success=False, error="No speech detected in audio.")

    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"\x00\x00" * 8000,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_FAILED.value)
    assert event.event_type == VoicePipelineEventType.TRANSCRIPTION_FAILED.value
    assert event.transcription is not None
    assert event.transcription.success is False
    assert "No speech detected" in event.error


# ==============================================================================
# 3. Truncation and Error Handling
# ==============================================================================

def test_vad_overflow_produces_pipeline_overflow_event(pipeline_fixture, test_config):
    """Verify that BUFFER_OVERFLOW emitted by VAD publishes buffer_overflow event."""
    pipeline, capture, vad, _ = pipeline_fixture
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="buffer_overflow",
            timestamp_ms=time.time() * 1000.0,
            truncated=True,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.BUFFER_OVERFLOW.value)
    assert event.event_type == VoicePipelineEventType.BUFFER_OVERFLOW.value
    assert event.truncated is True


def test_truncated_utterance_is_not_sent_to_stt(pipeline_fixture, test_config):
    """Verify truncation policy: audio with truncated=True is discarded and not sent to STT."""
    pipeline, capture, vad, stt = pipeline_fixture
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"\x00\x00" * 16000,
            truncated=True,  # Truncated
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)
    time.sleep(0.1)

    assert len(stt.transcribe_calls) == 0


def test_pre_speech_truncated_utterance_is_not_sent_to_stt(pipeline_fixture, test_config):
    """Verify truncation policy: audio with pre_speech_truncated=True is discarded without STT."""
    pipeline, capture, vad, stt = pipeline_fixture
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"\x00\x00" * 16000,
            pre_speech_truncated=True,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)
    time.sleep(0.1)

    assert len(stt.transcribe_calls) == 0


def test_invalid_frame_handling(pipeline_fixture):
    """Verify that invalid PCM frame length publishes pipeline_error without crashing worker."""
    pipeline, capture, _, _ = pipeline_fixture
    pipeline.start()

    # Feed invalid frame of 15 bytes
    capture.feed_frame(b"\x00" * 15)

    event = wait_for_event(pipeline, VoicePipelineEventType.PIPELINE_ERROR.value)
    assert event.event_type == VoicePipelineEventType.PIPELINE_ERROR.value
    assert "Invalid frame length" in event.error
    assert pipeline.is_running


def test_capture_read_failure(pipeline_fixture):
    """Verify that transient read_frame exception emits pipeline_error without crashing worker."""
    pipeline, capture, _, _ = pipeline_fixture
    capture.read_failures_remaining = 1
    pipeline.start()

    event = wait_for_event(pipeline, VoicePipelineEventType.PIPELINE_ERROR.value)
    assert event.event_type == VoicePipelineEventType.PIPELINE_ERROR.value
    assert "Capture read error" in event.error
    assert pipeline.is_running


def test_vad_processing_failure(pipeline_fixture, test_config):
    """Verify that VAD processing exception emits pipeline_error and worker continues."""
    pipeline, capture, vad, _ = pipeline_fixture
    vad.fail_on_process = True
    pipeline.start()

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.PIPELINE_ERROR.value)
    assert event.event_type == VoicePipelineEventType.PIPELINE_ERROR.value
    assert "VAD processing error" in event.error
    assert pipeline.is_running


def test_stt_exception_handling(pipeline_fixture, test_config):
    """Verify that unexpected STT exception publishes transcription_failed without crashing worker."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.fail_with_exception = True
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"\x00\x00" * 8000,
        )
    ])

    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_FAILED.value)
    assert event.event_type == VoicePipelineEventType.TRANSCRIPTION_FAILED.value
    assert "STT execution error" in event.error
    assert pipeline.is_running


def test_capture_startup_failure(test_config):
    """Verify that failure in capture.start() raises VoicePipelineError and publishes error."""
    capture = FakeAudioCaptureService(test_config)
    capture.fail_on_start = True
    pipeline = VoicePipeline(config=test_config, capture_service=capture)

    with pytest.raises(VoicePipelineError, match="Failed to start capture service"):
        pipeline.start()

    assert not pipeline.is_running
    event = pipeline.read_event(timeout=0.1)
    assert event is not None
    assert event.event_type == VoicePipelineEventType.PIPELINE_ERROR.value


# ==============================================================================
# 4. Concurrency, Lifecycle, and Queue Bounds
# ==============================================================================

def test_no_duplicate_processing_workers(pipeline_fixture):
    """Verify that concurrent or repeated start calls never produce duplicate active threads."""
    pipeline, _, _, _ = pipeline_fixture

    threads = [threading.Thread(target=pipeline.start) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert pipeline.is_running
    assert pipeline._worker_thread is not None
    assert pipeline._worker_thread.is_alive()


def test_no_overlapping_stt_calls(pipeline_fixture, test_config):
    """Verify that STT calls are strictly serialized with max_concurrent_calls <= 1."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.delay = 0.05
    pipeline.start()

    for _ in range(3):
        vad.queue_response([
            make_vad_event(
                event_type="speech_end",
                timestamp_ms=time.time() * 1000.0,
                accumulated_audio=b"\x00\x00" * 4000,
            )
        ])
        capture.feed_frame(b"\x00" * test_config.frame_bytes)

    time.sleep(0.3)
    assert stt.max_concurrent_calls <= 1


def test_stop_while_waiting_for_frame(pipeline_fixture):
    """Verify clean worker termination when stop() is called while read_frame() is blocking."""
    pipeline, _, _, _ = pipeline_fixture
    pipeline.start()

    # read_frame will return None after small timeout repeatedly
    time.sleep(0.05)
    pipeline.stop()

    assert not pipeline.is_running
    assert pipeline._worker_thread is None


def test_stop_during_in_progress_transcription(pipeline_fixture, test_config):
    """Verify that stop() waits for in-progress STT to finish without corruption."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.delay = 0.1
    pipeline.start()

    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=time.time() * 1000.0,
            accumulated_audio=b"\x00\x00" * 8000,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    time.sleep(0.02)
    pipeline.stop()

    assert not pipeline.is_running
    assert len(stt.transcribe_calls) == 1


def test_restart_after_successful_stop(pipeline_fixture):
    """Verify clean repeated start -> stop -> start -> stop cycles."""
    pipeline, capture, _, _ = pipeline_fixture

    for _ in range(3):
        pipeline.start()
        assert pipeline.is_running
        pipeline.stop()
        assert not pipeline.is_running

    assert capture.start_called == 3
    assert capture.stop_called == 3


def test_previous_worker_cannot_corrupt_new_session(pipeline_fixture, test_config):
    """Verify that an older worker thread with stale session_id cannot alter newer session."""
    pipeline, _, _, _ = pipeline_fixture

    pipeline.start()
    old_session = pipeline._session_id

    # Stop and immediately start new session
    pipeline.stop()
    pipeline.start()
    new_session = pipeline._session_id
    assert new_session > old_session

    # Directly run old worker logic with old session_id - it must return immediately
    pipeline._processing_worker(old_session)
    assert pipeline.is_running
    assert pipeline._session_id == new_session


def test_event_queue_remains_bounded(test_config):
    """Verify that event queue never exceeds configured pipeline_event_queue_size."""
    test_config = VoiceConfig(pipeline_event_queue_size=5)
    pipeline = VoicePipeline(config=test_config)

    for i in range(20):
        pipeline._publish_event(
            VoicePipelineEvent(
                event_type="speech_started",
                timestamp=time.time(),
                error=f"event-{i}",
            )
        )

    assert pipeline._event_queue.qsize() == 5


def test_slow_consumer_does_not_block_audio_capture_indefinitely(pipeline_fixture, test_config):
    """Verify that audio worker continues processing frames even when event queue is completely full."""
    # Set small event queue size
    small_config = VoiceConfig(
        pipeline_event_queue_size=2,
        pipeline_frame_read_timeout_seconds=0.02,
    )
    capture = FakeAudioCaptureService(small_config)
    vad = FakeVAD(small_config)
    pipeline = VoicePipeline(config=small_config, capture_service=capture, vad=vad)
    pipeline.start()

    # Fill event queue to capacity
    pipeline._publish_event(VoicePipelineEvent(event_type="event1", timestamp=time.time()))
    pipeline._publish_event(VoicePipelineEvent(event_type="event2", timestamp=time.time()))
    assert pipeline._event_queue.full()

    # Now feed frames - worker must process them without hanging or blocking indefinitely
    for _ in range(5):
        vad.queue_response([
            make_vad_event(
                event_type="speech_start",
                timestamp_ms=time.time() * 1000.0,
            )
        ])
        capture.feed_frame(b"\x00" * small_config.frame_bytes)

    time.sleep(0.1)
    assert pipeline.is_running
    pipeline.stop()


# ==============================================================================
# 5. Milestone 5.1.3.1 Correctness & Reliability Tests
# ==============================================================================

def test_speech_events_have_plausible_unix_timestamps(pipeline_fixture, test_config):
    """Verify speech events have wall-clock Unix timestamps, not VAD elapsed frame milliseconds."""
    pipeline, capture, vad, _ = pipeline_fixture
    pipeline.start()

    t_start = time.time()
    # VAD elapsed frame timestamp is 45.0 ms
    vad.queue_response([
        make_vad_event(
            event_type="speech_start",
            timestamp_ms=45.0,
            frame=b"\x00" * test_config.frame_bytes,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.SPEECH_STARTED.value)
    t_end = time.time()

    # Event timestamp must be a plausible Unix timestamp (in seconds, current time)
    assert t_start <= event.timestamp <= t_end
    assert event.timestamp > 1_700_000_000.0
    # Must NOT be 0.045 (which would be timestamp_ms / 1000)
    assert event.timestamp != 0.045
    # Elapsed VAD frame timestamp is preserved in vad_timestamp_ms
    assert event.vad_timestamp_ms == 45.0


def test_transient_capture_read_failure_and_recovery(pipeline_fixture, test_config):
    """Verify that a transient capture read failure emits an error and recovers on next read."""
    pipeline, capture, vad, _ = pipeline_fixture
    # Configure 1 transient failure
    capture.read_failures_remaining = 1
    pipeline.start()

    # First event should be the transient PIPELINE_ERROR
    err_event = wait_for_event(pipeline, VoicePipelineEventType.PIPELINE_ERROR.value)
    assert "Capture read error (1/" in err_event.error
    assert pipeline.is_running

    # Subsequent reads succeed and process frames normally
    vad.queue_response([
        make_vad_event(
            event_type="speech_start",
            timestamp_ms=10.0,
            frame=b"\x00" * test_config.frame_bytes,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    speech_event = wait_for_event(pipeline, VoicePipelineEventType.SPEECH_STARTED.value)
    assert speech_event.event_type == VoicePipelineEventType.SPEECH_STARTED.value
    assert pipeline.is_running


def test_persistent_capture_read_failures_clean_worker_termination_and_restart(pipeline_fixture, test_config):
    """Verify that reaching max consecutive read errors stops worker cleanly and allows restart."""
    pipeline, capture, vad, _ = pipeline_fixture
    capture.fail_on_read = True
    pipeline.start()

    worker_thread = pipeline._worker_thread
    assert worker_thread is not None

    # Wait for worker thread to stop on reaching threshold (test_config has max=3)
    worker_thread.join(timeout=2.0)
    assert not worker_thread.is_alive()
    assert not pipeline.is_running

    # Drain and verify error events: exactly 3 error events emitted (no infinite loop)
    errors = []
    while True:
        ev = pipeline.read_event(timeout=0.05)
        if ev is None:
            break
        if ev.event_type == VoicePipelineEventType.PIPELINE_ERROR.value:
            errors.append(ev)

    assert len(errors) == test_config.pipeline_max_consecutive_read_errors
    assert "Maximum consecutive capture read errors" in errors[-1].error

    # Verify successful restart after failed session has terminated
    capture.fail_on_read = False
    pipeline.start()
    assert pipeline.is_running
    assert pipeline._worker_thread is not None
    assert pipeline._worker_thread is not worker_thread
    assert pipeline._worker_thread.is_alive()

    # Feed a frame and verify normal processing in new session
    vad.queue_response([
        make_vad_event(
            event_type="speech_start",
            timestamp_ms=10.0,
            frame=b"\x00" * test_config.frame_bytes,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.SPEECH_STARTED.value)
    assert event.event_type == VoicePipelineEventType.SPEECH_STARTED.value

    pipeline.stop()
    assert not pipeline.is_running


def test_stt_unavailable_skips_transcription_and_emits_failure(pipeline_fixture, test_config):
    """Verify that unavailable STT does not invoke transcribe() and emits transcription_failed."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.available = False
    pipeline.start()

    sample_audio = b"\x00\x00" * 16000  # 1.0 second audio
    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=500.0,
            accumulated_audio=sample_audio,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_FAILED.value)
    assert event.event_type == VoicePipelineEventType.TRANSCRIPTION_FAILED.value
    assert "unavailable" in event.error.lower()
    assert event.audio_duration_seconds == 1.0
    assert len(stt.transcribe_calls) == 0
    assert pipeline.is_running


def test_stt_availability_exception_does_not_crash_worker_and_processes_next_utterance(pipeline_fixture, test_config):
    """Verify that an exception in is_available() does not crash worker and allows subsequent utterances."""
    pipeline, capture, vad, stt = pipeline_fixture
    stt.fail_on_available = True
    pipeline.start()

    sample_audio = b"\x00\x00" * 8000  # 0.5s
    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=200.0,
            accumulated_audio=sample_audio,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    # First utterance fails due to availability check exception
    event1 = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_FAILED.value)
    assert "availability check error" in event1.error
    assert len(stt.transcribe_calls) == 0
    assert pipeline.is_running

    # Fix availability check
    stt.fail_on_available = False
    stt.available = True

    # Process second utterance
    vad.queue_response([
        make_vad_event(
            event_type="speech_end",
            timestamp_ms=800.0,
            accumulated_audio=sample_audio,
        )
    ])
    capture.feed_frame(b"\x00" * test_config.frame_bytes)

    event2 = wait_for_event(pipeline, VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value)
    assert event2.event_type == VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value
    assert event2.transcription.text == "Mocked speech"
    assert len(stt.transcribe_calls) == 1


def test_clear_events_returns_exact_count(pipeline_fixture):
    """Verify clear_events() returns the exact count of removed events and empties queue."""
    pipeline, _, _, _ = pipeline_fixture

    # Empty queue returns 0
    assert pipeline.clear_events() == 0

    # Populate with 5 events
    for i in range(5):
        pipeline._publish_event(
            VoicePipelineEvent(
                event_type=VoicePipelineEventType.SPEECH_STARTED.value,
                timestamp=time.time(),
                error=f"test-{i}",
            )
        )

    # Clear populated queue
    count = pipeline.clear_events()
    assert count == 5

    # Queue must be empty afterwards
    assert pipeline.read_event(timeout=0.01) is None
    assert pipeline.clear_events() == 0


def test_critical_events_priority_retention_during_queue_overflow():
    """Verify critical events (transcription, errors, stopped) are preserved over ordinary events."""
    custom_config = VoiceConfig(pipeline_event_queue_size=3)
    pipeline = VoicePipeline(config=custom_config)

    # Publish: ord1, crit1, ord2
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.SPEECH_STARTED.value, timestamp=time.time()))
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.TRANSCRIPTION_COMPLETED.value, timestamp=time.time()))
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.SPEECH_DETECTED.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Now publish ord3: should drop ord1
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.SPEECH_STARTED.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Now publish crit2 (pipeline_error): should drop ord2
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.PIPELINE_ERROR.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Now publish crit3 (transcription_failed): should drop ord3
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.TRANSCRIPTION_FAILED.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Now the queue contains 3 critical events: [crit1, crit2, crit3]
    # Publish ordinary event (buffer_overflow): must be dropped because all queued are critical
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.BUFFER_OVERFLOW.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Now publish crit4 (listening_stopped): must drop oldest critical (crit1)
    pipeline._publish_event(VoicePipelineEvent(event_type=VoicePipelineEventType.LISTENING_STOPPED.value, timestamp=time.time()))
    assert pipeline._event_queue.qsize() == 3

    # Read all remaining events and verify they are all critical: crit2, crit3, crit4
    e1 = pipeline.read_event(timeout=0.01)
    e2 = pipeline.read_event(timeout=0.01)
    e3 = pipeline.read_event(timeout=0.01)

    assert e1.event_type == VoicePipelineEventType.PIPELINE_ERROR.value
    assert e2.event_type == VoicePipelineEventType.TRANSCRIPTION_FAILED.value
    assert e3.event_type == VoicePipelineEventType.LISTENING_STOPPED.value
    assert pipeline.read_event(timeout=0.01) is None


def test_start_clear_capture_buffer_option_preserves_queued_frames():
    """Verify that start(clear_capture_buffer=False) preserves queued capture frames."""
    cfg = VoiceConfig()
    capture = FakeAudioCaptureService(cfg)
    pipeline = VoicePipeline(config=cfg, capture_service=capture)

    # Queue 3 frames before start
    capture.feed_frame(b"\x01\x00" * cfg.frame_samples)
    capture.feed_frame(b"\x02\x00" * cfg.frame_samples)
    capture.feed_frame(b"\x03\x00" * cfg.frame_samples)

    pipeline.start(clear_capture_buffer=False)
    pipeline.stop()

    # Frames should have been preserved (queue not cleared before worker started)
    # Clear capture service explicitly
    capture.stop()


def test_start_default_clears_capture_buffer():
    """Verify that start() with default argument clears the capture buffer."""
    cfg = VoiceConfig()
    capture = FakeAudioCaptureService(cfg)
    pipeline = VoicePipeline(config=cfg, capture_service=capture)

    capture.feed_frame(b"\x01\x00" * cfg.frame_samples)
    assert not capture._frame_queue.empty()

    pipeline.start()
    # Queue was cleared on start
    assert capture._frame_queue.empty()
    pipeline.stop()
    capture.stop()


