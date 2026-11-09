"""
Automated Unit and Integration Tests for Local Piper TTS Subsystem (Milestone 5.1.6).
Verifies lazy loading, model validation, streaming chunk playback, native format preservation,
cancellation, session serialization, error handling, and parameter bounds.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from voice.audio_io import (
    AudioDeviceError,
    AudioOutputSink,
    AudioWriteError,
    SoundDeviceOutputSink,
)
from voice.config import VoiceConfig
from voice.tts import (
    PiperTTSService,
    TTSConfigError,
    TTSInitializationError,
    TTSPlaybackError,
    TTSUnavailableError,
)


# =============================================================================
# Test Doubles and Mocks
# =============================================================================

@dataclass
class MockPiperChunk:
    """Mock audio chunk matching Piper streaming API format."""
    audio_int16_bytes: bytes
    sample_rate: int = 22050
    sample_width: int = 2
    sample_channels: int = 1


class MockPiperVoice:
    """Controllable mock of PiperVoice instance."""

    def __init__(
        self,
        sample_rate: int = 22050,
        chunks: list[MockPiperChunk] | None = None,
        chunk_delay: float = 0.0,
        fail_during_synthesis: bool = False,
    ):
        self.sample_rate = sample_rate
        self.chunks = chunks if chunks is not None else [
            MockPiperChunk(audio_int16_bytes=b"\x01\x00" * 320, sample_rate=sample_rate),
            MockPiperChunk(audio_int16_bytes=b"\x02\x00" * 320, sample_rate=sample_rate),
            MockPiperChunk(audio_int16_bytes=b"\x03\x00" * 320, sample_rate=sample_rate),
        ]
        self.chunk_delay = chunk_delay
        self.fail_during_synthesis = fail_during_synthesis
        self.synthesize_called = 0
        self.last_text = None
        self.last_syn_config = None

    def synthesize(self, text: str, syn_config: any = None):
        self.synthesize_called += 1
        self.last_text = text
        self.last_syn_config = syn_config

        for i, chunk in enumerate(self.chunks):
            if self.fail_during_synthesis and i == 1:
                raise RuntimeError("Simulated failure during synthesis stream")
            if self.chunk_delay > 0:
                time.sleep(self.chunk_delay)
            yield chunk


class MockAudioOutputSink(AudioOutputSink):
    """Controllable mock audio sink capturing format and chunks."""

    def __init__(
        self,
        fail_on_open: bool = False,
        fail_on_write: bool = False,
        write_delay: float = 0.0,
        block_on_write_event: threading.Event | None = None,
    ):
        self.fail_on_open = fail_on_open
        self.fail_on_write = fail_on_write
        self.write_delay = write_delay
        self.block_on_write_event = block_on_write_event

        self._active = False
        self.opened_sample_rate = None
        self.opened_channels = None
        self.open_called = 0
        self.stop_called = 0
        self.close_called = 0
        self.written_chunks: list[bytes] = []

        self._lock = threading.Lock()
        self.active_writers = 0
        self.max_concurrent_writers = 0

    def open(self, sample_rate: int | None = None, channels: int | None = None) -> None:
        if self.fail_on_open:
            raise AudioDeviceError("Simulated output device open failure")
        self._active = True
        self.open_called += 1
        self.opened_sample_rate = sample_rate
        self.opened_channels = channels

    def write(self, pcm_data: bytes) -> None:
        with self._lock:
            self.active_writers += 1
            if self.active_writers > self.max_concurrent_writers:
                self.max_concurrent_writers = self.active_writers

        try:
            if not self._active:
                raise AudioWriteError("Cannot write to inactive sink")
            if self.fail_on_write:
                raise AudioDeviceError("Simulated output device write failure")

            if self.block_on_write_event is not None:
                self.block_on_write_event.wait(timeout=2.0)

            if self.write_delay > 0:
                time.sleep(self.write_delay)

            self.written_chunks.append(pcm_data)
        finally:
            with self._lock:
                self.active_writers -= 1

    def stop(self) -> None:
        self.stop_called += 1
        # If write was blocking on event, unblock it
        if self.block_on_write_event is not None:
            self.block_on_write_event.set()

    def close(self) -> None:
        self._active = False
        self.close_called += 1

    @property
    def is_active(self) -> bool:
        return self._active


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_voice_files(tmp_path):
    """Create dummy local .onnx and .onnx.json files for testing validation."""
    model_file = tmp_path / "test_voice.onnx"
    model_file.write_bytes(b"dummy onnx content")

    config_file = tmp_path / "test_voice.onnx.json"
    config_data = {
        "audio": {"sample_rate": 22050},
        "espeak": {"voice": "en-us"},
        "piper_version": "1.0.0",
    }
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    return str(model_file), str(config_file)


@pytest.fixture
def base_tts_config(temp_voice_files):
    model_path, config_path = temp_voice_files
    return VoiceConfig(
        tts_model_path=model_path,
        tts_config_path=config_path,
        tts_max_text_length=500,
        tts_max_audio_duration_seconds=10.0,
        tts_playback_join_timeout_seconds=1.0,
    )


# =============================================================================
# 1. Dependency, Model Loading and Caching Tests
# =============================================================================

def test_missing_piper_dependency(temp_voice_files):
    """Verify that if piper-tts is not installed, service gracefully reports unavailable."""
    model_path, config_path = temp_voice_files
    cfg = VoiceConfig(tts_model_path=model_path, tts_config_path=config_path)

    with patch.dict("sys.modules", {"piper": None}):
        service = PiperTTSService(config=cfg)
        assert service.is_available() is False
        assert "piper-tts" in (service.initialization_error or "").lower()

        with pytest.raises(TTSUnavailableError, match="piper-tts"):
            service.speak("Hello world")


def test_unconfigured_model_path_makes_service_unavailable():
    """Verify that when no TTS model path is configured, is_available returns False."""
    cfg = VoiceConfig(tts_model_path=None)
    service = PiperTTSService(config=cfg)
    assert service.is_available() is False
    assert "not configured" in (service.initialization_error or "").lower()

    with pytest.raises(TTSUnavailableError, match="not configured"):
        service.speak("Test speech")


def test_missing_model_file_on_disk(tmp_path):
    """Verify that a non-existent .onnx file is cleanly detected and reported."""
    non_existent = str(tmp_path / "non_existent.onnx")
    cfg = VoiceConfig(tts_model_path=non_existent)
    service = PiperTTSService(config=cfg)

    assert service.is_available() is False
    assert "not found" in (service.initialization_error or "").lower()


def test_missing_config_json_file_on_disk(tmp_path):
    """Verify that missing matching .onnx.json file prevents loading."""
    model_file = tmp_path / "test.onnx"
    model_file.write_bytes(b"dummy")

    cfg = VoiceConfig(tts_model_path=str(model_file))  # Implicit config test.onnx.json missing
    service = PiperTTSService(config=cfg)

    assert service.is_available() is False
    assert "configuration file not found" in (service.initialization_error or "").lower()


def test_invalid_json_in_config_file(tmp_path):
    """Verify that corrupted JSON config produces clear initialization error."""
    model_file = tmp_path / "test.onnx"
    model_file.write_bytes(b"dummy")
    config_file = tmp_path / "test.onnx.json"
    config_file.write_text("{corrupt_json: true,", encoding="utf-8")

    cfg = VoiceConfig(tts_model_path=str(model_file))
    service = PiperTTSService(config=cfg)

    assert service.is_available() is False
    assert "Failed to parse TTS voice model JSON" in (service.initialization_error or "")


def test_non_dict_json_root_rejected(tmp_path):
    """Verify JSON configuration root must be an object/dict."""
    model_file = tmp_path / "test.onnx"
    model_file.write_bytes(b"dummy")
    config_file = tmp_path / "test.onnx.json"
    config_file.write_text("[\"invalid\", \"root\"]", encoding="utf-8")

    cfg = VoiceConfig(tts_model_path=str(model_file))
    service = PiperTTSService(config=cfg)

    assert service.is_available() is False
    assert "root must be JSON object" in (service.initialization_error or "")


def test_model_loading_success_and_caching(base_tts_config):
    """Verify that model is lazily loaded on first use and cached across calls."""
    mock_voice = MockPiperVoice(sample_rate=22050)
    loader_calls = 0

    def mock_loader(m_path, c_path):
        nonlocal loader_calls
        loader_calls += 1
        return mock_voice

    service = PiperTTSService(config=base_tts_config, voice_loader=mock_loader)
    assert loader_calls == 0  # Lazy: not loaded yet

    # First access loads
    assert service.is_available() is True
    assert loader_calls == 1

    # Second access reuses cached model
    assert service.is_available() is True
    assert loader_calls == 1


def test_model_loading_failure_records_initialization_error(base_tts_config):
    """Verify that loader exceptions are captured and expose meaningful error."""
    def failing_loader(m_path, c_path):
        raise ValueError("Invalid ONNX graph structure in voice model")

    service = PiperTTSService(config=base_tts_config, voice_loader=failing_loader)
    assert service.is_available() is False
    assert "Invalid ONNX graph structure" in (service.initialization_error or "")


# =============================================================================
# 2. Text Input Validation Tests
# =============================================================================

def test_empty_and_whitespace_text_rejected(base_tts_config):
    """Verify empty or whitespace-only text raises TTSConfigError."""
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, voice=mock_voice)

    with pytest.raises(TTSConfigError, match="Cannot synthesize empty"):
        service.speak("")

    with pytest.raises(TTSConfigError, match="Cannot synthesize empty"):
        service.speak("    \n\t  ")

    with pytest.raises(TTSConfigError, match="non-empty string"):
        service.speak(None)


def test_oversized_text_rejected(base_tts_config):
    """Verify text exceeding tts_max_text_length is rejected with clear limit error."""
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, voice=mock_voice)

    long_text = "A" * (base_tts_config.tts_max_text_length + 10)
    with pytest.raises(TTSConfigError, match="exceeds maximum allowed"):
        service.speak(long_text)


# =============================================================================
# 3. Audio Streaming, Sequential Ordering, and Format Preservation
# =============================================================================

def test_streaming_chunks_delivered_in_order(base_tts_config):
    """Verify chunks from Piper streaming API are written to the sink in sequential order."""
    chunks = [
        MockPiperChunk(audio_int16_bytes=b"CHUNK_01"),
        MockPiperChunk(audio_int16_bytes=b"CHUNK_02"),
        MockPiperChunk(audio_int16_bytes=b"CHUNK_03"),
    ]
    mock_voice = MockPiperVoice(chunks=chunks)
    mock_sink = MockAudioOutputSink()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Test chunk order", non_blocking=False)

    assert mock_sink.written_chunks == [b"CHUNK_01", b"CHUNK_02", b"CHUNK_03"]


def test_native_piper_sample_rate_and_format_preservation(base_tts_config):
    """Verify non-16-kHz sample rates (e.g. 22050 Hz) are passed to output sink."""
    mock_voice = MockPiperVoice(sample_rate=22050)
    mock_sink = MockAudioOutputSink()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Speaking at 22050 Hz", non_blocking=False)

    assert mock_sink.opened_sample_rate == 22050
    assert mock_sink.opened_channels == 1


def test_custom_piper_output_rate_44100(base_tts_config):
    """Verify high-quality 44.1 kHz Piper models configure speaker sink accordingly."""
    chunks = [MockPiperChunk(audio_int16_bytes=b"\x00" * 400, sample_rate=44100)]
    mock_voice = MockPiperVoice(sample_rate=44100, chunks=chunks)
    mock_sink = MockAudioOutputSink()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Speaking at 44100 Hz", non_blocking=False)

    assert mock_sink.opened_sample_rate == 44100


# =============================================================================
# 4. Playback Lifecycle: Blocking vs Non-Blocking
# =============================================================================

def test_non_blocking_playback_starts_worker(base_tts_config):
    """Verify non_blocking=True spawns a daemon thread and is_speaking is True."""
    block_write_event = threading.Event()
    mock_sink = MockAudioOutputSink(block_on_write_event=block_write_event)
    mock_voice = MockPiperVoice()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Non blocking speech", non_blocking=True)

    assert service.is_speaking is True

    # Unblock write and wait for completion
    block_write_event.set()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not service.is_speaking:
            break
        time.sleep(0.02)

    assert service.is_speaking is False
    assert len(mock_sink.written_chunks) == 3


def test_blocking_playback_waits_for_completion(base_tts_config):
    """Verify non_blocking=False blocks caller until all audio is written."""
    mock_sink = MockAudioOutputSink()
    mock_voice = MockPiperVoice()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Blocking speech", non_blocking=False)

    assert service.is_speaking is False
    assert len(mock_sink.written_chunks) == 3


# =============================================================================
# 5. Cancellation and Barge-In
# =============================================================================

def test_stop_cancels_active_playback(base_tts_config):
    """Verify stop() immediately halts active playback and stops sink."""
    block_write_event = threading.Event()
    mock_sink = MockAudioOutputSink(block_on_write_event=block_write_event)
    mock_voice = MockPiperVoice(chunks=[
        MockPiperChunk(audio_int16_bytes=b"C1"),
        MockPiperChunk(audio_int16_bytes=b"C2"),
        MockPiperChunk(audio_int16_bytes=b"C3"),
    ])

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Long running speech", non_blocking=True)

    assert service.is_speaking is True
    service.stop()

    assert service.is_speaking is False
    assert mock_sink.stop_called >= 1


def test_stop_during_synthesis_stream(base_tts_config):
    """Verify calling stop() while voice is yielding chunks aborts stream early."""
    started_streaming = threading.Event()

    class StallingVoice:
        def __init__(self):
            self.sample_rate = 22050

        def synthesize(self, text, syn_config=None):
            yield MockPiperChunk(audio_int16_bytes=b"CH_1")
            started_streaming.set()
            time.sleep(0.3)
            yield MockPiperChunk(audio_int16_bytes=b"CH_2")

    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=StallingVoice())
    service.speak("Stream test", non_blocking=True)

    assert started_streaming.wait(timeout=2.0) is True
    service.stop()

    assert service.is_speaking is False
    assert len(mock_sink.written_chunks) == 1  # CH_2 never written


def test_repeated_speak_and_stop_cycles(base_tts_config):
    """Verify that multiple consecutive speak() and stop() cycles execute cleanly."""
    mock_voice = MockPiperVoice()
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    for i in range(3):
        service.speak(f"Cycle number {i}", non_blocking=False)
        assert service.is_speaking is False
        service.stop()  # Idempotent stop
        assert service.is_speaking is False

    assert len(mock_sink.written_chunks) == 9


# =============================================================================
# 6. Concurrency and Session Preemption
# =============================================================================

def test_preemptive_speak_cancels_previous_session(base_tts_config):
    """Verify that starting a new speak() while one is active cancels the prior session."""
    block_event = threading.Event()
    mock_sink = MockAudioOutputSink(block_on_write_event=block_event)
    mock_voice = MockPiperVoice(chunks=[
        MockPiperChunk(audio_int16_bytes=b"S1_A"),
        MockPiperChunk(audio_int16_bytes=b"S1_B"),
    ])

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("First utterance", non_blocking=True)
    assert service.is_speaking is True

    # Second speak request arrives before first finishes
    mock_sink.block_on_write_event = None
    mock_voice.chunks = [MockPiperChunk(audio_int16_bytes=b"S2_A")]

    service.speak("Second utterance", non_blocking=False)

    assert service.is_speaking is False
    assert mock_sink.stop_called >= 1


def test_no_concurrent_writers_to_sink(base_tts_config):
    """Verify maximum concurrent writers to output sink never exceeds 1."""
    mock_sink = MockAudioOutputSink(write_delay=0.01)
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    threads = []
    for i in range(5):
        t = threading.Thread(target=lambda idx=i: service.speak(f"Concurrent {idx}", non_blocking=False))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert mock_sink.max_concurrent_writers <= 1


# =============================================================================
# 7. Error Handling and Limits
# =============================================================================

def test_worker_synthesis_exception_captured_in_last_error(base_tts_config):
    """Verify exceptions during synthesis worker are captured in last_error and clear is_speaking."""
    mock_voice = MockPiperVoice(fail_during_synthesis=True)
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    service.speak("Will fail mid stream", non_blocking=True)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if service.last_error is not None and not service.is_speaking:
            break
        time.sleep(0.02)

    assert service.is_speaking is False
    assert "Simulated failure during synthesis stream" in str(service.last_error)


def test_blocking_speak_raises_underlying_exception(base_tts_config):
    """Verify blocking speak() raises the error directly to the caller."""
    mock_voice = MockPiperVoice(fail_during_synthesis=True)
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    with pytest.raises(RuntimeError, match="Simulated failure during synthesis stream"):
        service.speak("Fail directly", non_blocking=False)

    assert service.is_speaking is False


def test_sink_device_write_error_handled(base_tts_config):
    """Verify device write error sets last_error and resets is_speaking."""
    mock_sink = MockAudioOutputSink(fail_on_write=True)
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    service.speak("Device fails", non_blocking=True)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if service.last_error is not None and not service.is_speaking:
            break
        time.sleep(0.02)

    assert service.is_speaking is False
    assert isinstance(service.last_error, AudioDeviceError)


def test_generated_audio_duration_limit_exceeded(base_tts_config):
    """Verify audio exceeding tts_max_audio_duration_seconds raises TTSConfigError."""
    # 22050 Hz * 2 bytes * 1 ch = 44100 bytes/sec
    # max duration 10.0s -> 441000 bytes max
    huge_chunk = MockPiperChunk(audio_int16_bytes=b"\x00" * 500000, sample_rate=22050)
    mock_voice = MockPiperVoice(chunks=[huge_chunk])
    mock_sink = MockAudioOutputSink()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    with pytest.raises(TTSConfigError, match="Generated audio exceeded maximum limit"):
        service.speak("Generates huge audio", non_blocking=False)


def test_uncooperative_worker_timeout_during_stop(base_tts_config):
    """Verify that if a worker thread refuses to stop, stop() raises TTSPlaybackError."""
    stubborn_lock = threading.Lock()
    stubborn_lock.acquire()
    entered_stubborn = threading.Event()

    class StubbornVoice:
        def __init__(self):
            self.sample_rate = 22050

        def synthesize(self, text, syn_config=None):
            yield MockPiperChunk(audio_int16_bytes=b"DATA")
            # Signal that worker has yielded chunk 1 and is entering stubborn hang
            entered_stubborn.set()
            stubborn_lock.acquire()

    cfg = VoiceConfig(
        tts_model_path=base_tts_config.tts_model_path,
        tts_config_path=base_tts_config.tts_config_path,
        tts_playback_join_timeout_seconds=0.1,  # Short timeout for test
    )
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=cfg, sink=mock_sink, voice=StubbornVoice())

    service.speak("Stubborn test", non_blocking=True)
    assert entered_stubborn.wait(timeout=3.0), "Worker did not reach stubborn section in time"

    try:
        with pytest.raises(TTSPlaybackError, match="failed to terminate within"):
            service.stop()
    finally:
        stubborn_lock.release()


# =============================================================================
# 8. Deterministic Concurrency & Hardening Tests (Milestone 5.1.6.1)
# =============================================================================

def test_concurrent_speak_requests_cannot_overwrite_active_session(base_tts_config):
    """Verify simultaneous speak() calls serialize cleanly without overwriting sessions."""
    barrier = threading.Barrier(2)
    mock_sink = MockAudioOutputSink(write_delay=0.01)
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    threads = []
    errors = []

    def call_speak(text):
        try:
            barrier.wait(timeout=2.0)
            service.speak(text, non_blocking=True)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=call_speak, args=("Utterance Alpha",))
    t2 = threading.Thread(target=call_speak, args=("Utterance Beta",))
    threads.extend([t1, t2])

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Wait for the winning/final playback to complete
    deadline = time.time() + 3.0
    while time.time() < deadline and service.is_speaking:
        time.sleep(0.01)

    assert service.is_speaking is False
    assert mock_sink.max_concurrent_writers <= 1
    assert service._session_counter == 2


def test_previous_session_stopped_and_joined_before_next_session_begins(base_tts_config):
    """Verify previous worker thread is confirmed terminated before new session begins."""
    s1_started = threading.Event()
    s1_unblock = threading.Event()
    worker_1_ref = []
    w1_was_alive_during_s2_start = []

    class SteppedVoice:
        def __init__(self):
            self.sample_rate = 22050

        def synthesize(self, text, syn_config=None):
            if "Session 1" in text:
                s1_started.set()
                s1_unblock.wait(timeout=2.0)
                yield MockPiperChunk(audio_int16_bytes=b"S1_CH01")
            else:
                # Session 2 verifies worker 1 is dead
                if worker_1_ref and worker_1_ref[0].is_alive():
                    w1_was_alive_during_s2_start.append(True)
                yield MockPiperChunk(audio_int16_bytes=b"S2_CH01")

    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=SteppedVoice())

    service.speak("Session 1", non_blocking=True)
    assert s1_started.wait(timeout=2.0) is True

    # Capture worker 1 thread
    worker_1_ref.append(service._current_session.worker_thread)
    assert worker_1_ref[0].is_alive() is True

    # Unblock S1 so it can exit on cancellation
    s1_unblock.set()

    # Launch Session 2
    service.speak("Session 2", non_blocking=True)

    deadline = time.time() + 2.0
    while time.time() < deadline and service.is_speaking:
        time.sleep(0.01)

    assert not w1_was_alive_during_s2_start
    assert worker_1_ref[0].is_alive() is False


def test_concurrent_stop_and_speak_coordination(base_tts_config):
    """Verify simultaneous stop() and speak() operations do not corrupt session ownership."""
    barrier = threading.Barrier(2)
    mock_sink = MockAudioOutputSink()
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    errors = []

    def do_speak():
        try:
            barrier.wait(timeout=2.0)
            service.speak("Concurrent race", non_blocking=True)
        except Exception as e:
            errors.append(e)

    def do_stop():
        try:
            barrier.wait(timeout=2.0)
            service.stop()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=do_speak)
    t2 = threading.Thread(target=do_stop)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors

    # Clean up any active session if speak won after stop
    if service.is_speaking:
        service.stop()

    assert service.is_speaking is False
    assert mock_sink.max_concurrent_writers <= 1


def test_uncooperative_worker_timeout_prevents_replacement_session(base_tts_config):
    """Verify stubborn worker timeout prevents starting a replacement session and preserves worker ref."""
    stubborn_lock = threading.Lock()
    stubborn_lock.acquire()
    entered_stubborn = threading.Event()

    class StubbornVoice:
        def __init__(self):
            self.sample_rate = 22050

        def synthesize(self, text, syn_config=None):
            yield MockPiperChunk(audio_int16_bytes=b"HANG")
            entered_stubborn.set()
            stubborn_lock.acquire()

    cfg = VoiceConfig(
        tts_model_path=base_tts_config.tts_model_path,
        tts_config_path=base_tts_config.tts_config_path,
        tts_playback_join_timeout_seconds=0.1,
    )
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=cfg, sink=mock_sink, voice=StubbornVoice())

    service.speak("Stubborn session", non_blocking=True)
    assert entered_stubborn.wait(timeout=2.0) is True

    original_worker = service._current_session.worker_thread
    assert original_worker.is_alive() is True

    try:
        with pytest.raises(TTSPlaybackError, match="previous session failed to terminate within timeout"):
            service.speak("Replacement session", non_blocking=True)

        # Confirm session was not overwritten by Replacement session
        assert service._current_session.text == "Stubborn session"
        assert service._current_session.worker_thread is original_worker
    finally:
        stubborn_lock.release()
        service.stop()


def test_blocking_playback_cancellable_from_another_thread(base_tts_config):
    """Verify a blocking speak() call can be cancelled from another thread."""
    started_playing = threading.Event()

    class SlowVoice:
        def __init__(self):
            self.sample_rate = 22050

        def synthesize(self, text, syn_config=None):
            yield MockPiperChunk(audio_int16_bytes=b"SLOW_001")
            started_playing.set()
            for _ in range(10):
                time.sleep(0.05)
                yield MockPiperChunk(audio_int16_bytes=b"SLOW_002")

    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=SlowVoice())

    def cancel_thread():
        if started_playing.wait(timeout=2.0):
            service.stop()

    t_cancel = threading.Thread(target=cancel_thread)
    t_cancel.start()

    # Blocking call should return promptly once stop is called
    start_time = time.time()
    service.speak("Blocking slow speech", non_blocking=False)
    elapsed = time.time() - start_time

    t_cancel.join()
    assert service.is_speaking is False
    assert elapsed < 1.0  # Should abort well before 10 * 0.05s finishes


def test_thread_start_failure_leaves_no_stale_session(base_tts_config):
    """Verify thread start failure cleans up active-session state completely."""
    mock_sink = MockAudioOutputSink()
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)

    with patch.object(threading.Thread, "start", side_effect=RuntimeError("Cannot start thread")):
        with pytest.raises(TTSPlaybackError, match="Failed to start TTS worker thread"):
            service.speak("Will fail thread start", non_blocking=True)

    assert service.is_speaking is False
    assert service._current_session is None
    assert isinstance(service.last_error, RuntimeError)


# =============================================================================
# 9. Strict Audio Format Validation Tests (Milestone 5.1.6.1)
# =============================================================================

def test_invalid_sample_rate_rejected(base_tts_config):
    """Verify non-positive, boolean, float or non-integer sample rates raise TTSConfigError."""
    # Zero sample rate
    zero_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_rate=0)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[zero_chunk]))
    with pytest.raises(TTSConfigError, match="Invalid sample rate 0"):
        service.speak("Bad sample rate", non_blocking=False)

    # Negative sample rate
    neg_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_rate=-22050)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[neg_chunk]))
    with pytest.raises(TTSConfigError, match="Invalid sample rate -22050"):
        service.speak("Bad sample rate", non_blocking=False)

    # Boolean sample rate
    bool_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_rate=True)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[bool_chunk]))
    with pytest.raises(TTSConfigError, match="sample_rate must be an exact positive integer"):
        service.speak("Bad sample rate", non_blocking=False)

    # Floating point sample rate
    float_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_rate=22050.0)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[float_chunk]))
    with pytest.raises(TTSConfigError, match="sample_rate must be an exact positive integer"):
        service.speak("Bad sample rate", non_blocking=False)


def test_unsupported_sample_width_rejected(base_tts_config):
    """Verify non-16-bit, boolean, or float sample widths raise TTSConfigError."""
    # Non-2 width
    bad_width_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_width=1)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[bad_width_chunk]))
    with pytest.raises(TTSConfigError, match="Unsupported sample width 1"):
        service.speak("Bad width", non_blocking=False)

    # Boolean width (isinstance(True, int) is True)
    bool_width_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_width=True)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[bool_width_chunk]))
    with pytest.raises(TTSConfigError, match="only 16-bit .* as exact int"):
        service.speak("Bad width", non_blocking=False)

    # Float width
    float_width_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_width=2.0)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[float_width_chunk]))
    with pytest.raises(TTSConfigError, match="only 16-bit .* as exact int"):
        service.speak("Bad width", non_blocking=False)


def test_unsupported_channel_count_rejected(base_tts_config):
    """Verify multi-channel, zero-channel, boolean, or float channels raise TTSConfigError."""
    # Stereo
    stereo_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_channels=2)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[stereo_chunk]))
    with pytest.raises(TTSConfigError, match="Unsupported channel count 2"):
        service.speak("Stereo rejected", non_blocking=False)

    # Boolean channel (isinstance(True, int) is True and True == 1)
    bool_channel_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_channels=True)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[bool_channel_chunk]))
    with pytest.raises(TTSConfigError, match="only mono .* as exact int"):
        service.speak("Bool channel rejected", non_blocking=False)

    # Float channel (1.0 == 1)
    float_channel_chunk = MockPiperChunk(audio_int16_bytes=b"DATA", sample_channels=1.0)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[float_channel_chunk]))
    with pytest.raises(TTSConfigError, match="only mono .* as exact int"):
        service.speak("Float channel rejected", non_blocking=False)


def test_misaligned_pcm_byte_length_rejected(base_tts_config):
    """Verify PCM byte lengths not aligned to 2 bytes raise TTSConfigError."""
    odd_chunk = MockPiperChunk(audio_int16_bytes=b"ODD")
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[odd_chunk]))
    with pytest.raises(TTSConfigError, match="Misaligned PCM audio chunk"):
        service.speak("Odd byte length", non_blocking=False)


def test_format_change_between_consecutive_chunks_rejected(base_tts_config):
    """Verify changing audio format mid-stream raises TTSConfigError."""
    c1 = MockPiperChunk(audio_int16_bytes=b"CH_A", sample_rate=22050)
    c2 = MockPiperChunk(audio_int16_bytes=b"CH_B", sample_rate=16000)
    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[c1, c2]))

    with pytest.raises(TTSConfigError, match="Audio chunk format changed mid-stream"):
        service.speak("Format changes mid stream", non_blocking=False)


def test_sounddevice_sink_validates_audio_format():
    """Verify SoundDeviceOutputSink rejects invalid sample rates, channels, and types."""
    sink = SoundDeviceOutputSink()

    # Invalid sample rate
    with pytest.raises(AudioDeviceError, match="Invalid output sample rate 0"):
        sink.open(sample_rate=0)

    # Boolean sample rate (True evaluates to 1, but must be rejected)
    with pytest.raises(AudioDeviceError, match="Invalid output sample rate True"):
        sink.open(sample_rate=True)

    # Float sample rate
    with pytest.raises(AudioDeviceError, match="Invalid output sample rate 22050.0"):
        sink.open(sample_rate=22050.0)

    # Invalid channels (multi-channel)
    with pytest.raises(AudioDeviceError, match="Unsupported output channel count 2"):
        sink.open(channels=2)

    # Boolean channels (True evaluates to 1, but must be rejected)
    with pytest.raises(AudioDeviceError, match="Unsupported output channel count True"):
        sink.open(channels=True)

    # Float channels
    with pytest.raises(AudioDeviceError, match="Unsupported output channel count 1.0"):
        sink.open(channels=1.0)

    # String channels
    with pytest.raises(AudioDeviceError, match="Unsupported output channel count mono"):
        sink.open(channels="mono")

    # Inactive write
    with pytest.raises(AudioWriteError, match="Cannot write to closed or inactive"):
        sink.write(b"DATA")


def test_parameterized_sink_open_type_error_is_not_retried(base_tts_config):
    """Verify TypeError from parameterized sink.open() is not caught and retried parameterless."""
    class StrictParamSink(MockAudioOutputSink):
        def __init__(self):
            super().__init__()
            self.parameterless_open_called = False

        def open(self, sample_rate: int | None = None, channels: int | None = None) -> None:
            if sample_rate is not None or channels is not None:
                raise TypeError("Simulated sink signature failure on parameterized open")
            self.parameterless_open_called = True

    strict_sink = StrictParamSink()
    mock_voice = MockPiperVoice()
    service = PiperTTSService(config=base_tts_config, sink=strict_sink, voice=mock_voice)

    with pytest.raises(TypeError, match="Simulated sink signature failure on parameterized open"):
        service.speak("Should fail without fallback retry", non_blocking=False)

    assert strict_sink.parameterless_open_called is False


def test_malformed_audio_chunk_payload_rejected(base_tts_config):
    """Verify non-bytes audio fields or unsupported chunk payloads raise TTSConfigError."""
    # Chunk with int in audio_int16_bytes
    class BadAudioFieldChunk:
        audio_int16_bytes = 12345
        sample_rate = 22050
        sample_width = 2
        sample_channels = 1

    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[BadAudioFieldChunk()]))
    with pytest.raises(TTSConfigError, match="Piper audio chunk field must contain bytes-like data"):
        service.speak("Bad audio field", non_blocking=False)

    # Arbitrary object as chunk
    service2 = PiperTTSService(config=base_tts_config, voice=MockPiperVoice(chunks=[{"not": "a chunk"}]))
    with pytest.raises(TTSConfigError, match="Unsupported Piper chunk payload type"):
        service2.speak("Bad chunk object", non_blocking=False)


def test_empty_audio_chunk_behavior(base_tts_config):
    """Verify empty audio chunks are safely skipped without error or bogus writes."""
    empty_chunk = MockPiperChunk(audio_int16_bytes=b"")
    valid_chunk = MockPiperChunk(audio_int16_bytes=b"VALID_01")
    mock_sink = MockAudioOutputSink()
    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=MockPiperVoice(chunks=[empty_chunk, valid_chunk]))

    service.speak("Test empty chunk skipping", non_blocking=False)
    assert mock_sink.written_chunks == [b"VALID_01"]


def test_synthesis_config_construction_failure_surfaced(base_tts_config):
    """Verify Piper synthesis config errors are surfaced as TTSConfigError."""
    import sys
    import types

    service = PiperTTSService(config=base_tts_config, voice=MockPiperVoice())

    mock_piper = types.ModuleType("piper")
    mock_piper.SynthesisConfig = MagicMock(side_effect=ValueError("Invalid phoneme noise"))

    with patch.dict(sys.modules, {"piper": mock_piper}):
        with pytest.raises(TTSConfigError, match="Failed to construct Piper synthesis configuration"):
            service.speak("Test syn config failure", non_blocking=False)


# =============================================================================
# 10. Configuration Bounds & Import Verification
# =============================================================================

def test_voice_config_tts_parameter_validation():
    """Verify VoiceConfig rejects invalid TTS bounds."""
    with pytest.raises(ValueError, match="tts_max_text_length must be positive"):
        VoiceConfig(tts_max_text_length=0)

    with pytest.raises(ValueError, match="tts_max_audio_duration_seconds must be positive"):
        VoiceConfig(tts_max_audio_duration_seconds=-1.0)

    with pytest.raises(ValueError, match="tts_playback_join_timeout_seconds must be positive"):
        VoiceConfig(tts_playback_join_timeout_seconds=0)

    with pytest.raises(ValueError, match="tts_volume must be positive"):
        VoiceConfig(tts_volume=0.0)

    with pytest.raises(ValueError, match="tts_length_scale must be positive"):
        VoiceConfig(tts_length_scale=-0.5)

    with pytest.raises(ValueError, match="tts_noise_scale cannot be negative"):
        VoiceConfig(tts_noise_scale=-0.1)

    with pytest.raises(ValueError, match="tts_model_path cannot be empty"):
        VoiceConfig(tts_model_path="   ")


def test_import_voice_and_main_without_piper():
    """Verify importing voice and main succeeds cleanly without piper installed."""
    import main
    from voice import PiperTTSService, VoiceConfig

    assert PiperTTSService is not None
    assert VoiceConfig is not None


