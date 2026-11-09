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

from voice.audio_io import AudioDeviceError, AudioOutputSink, AudioWriteError
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
        MockPiperChunk(audio_int16_bytes=b"CHUNK_1"),
        MockPiperChunk(audio_int16_bytes=b"CHUNK_2"),
        MockPiperChunk(audio_int16_bytes=b"CHUNK_3"),
    ]
    mock_voice = MockPiperVoice(chunks=chunks)
    mock_sink = MockAudioOutputSink()

    service = PiperTTSService(config=base_tts_config, sink=mock_sink, voice=mock_voice)
    service.speak("Test chunk order", non_blocking=False)

    assert mock_sink.written_chunks == [b"CHUNK_1", b"CHUNK_2", b"CHUNK_3"]


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
# 8. Configuration Bounds & Import Verification
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

