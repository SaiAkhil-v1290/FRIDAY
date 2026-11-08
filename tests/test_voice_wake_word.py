"""
Unit and integration tests for JARVIS Local Wake-Word Detection Subsystem (Milestone 5.1.4).
Verifies detection logic, cooldown handling, duplicate prevention, bounded buffers,
engine abstraction, offline execution, zero Whisper usage, and zero cloud dependencies.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from voice.config import VoiceConfig
from voice.audio_io import AudioCaptureService
from voice.stt import SpeechToTextService
from voice.wake_word import (
    WakeWordDetector,
    WakeWordResult,
    WakeWordError,
    WakeWordModelError,
    WakeWordEngineError,
)


class MockWakeWordEngine:
    """Configurable mock wake-word engine for deterministic testing."""

    def __init__(self, return_score: float = 0.0) -> None:
        self.return_score = return_score
        self.calls: list[bytes] = []
        self.return_dict: dict[str, float] | None = None
        self.fail_with_exception: Exception | None = None
        self.reset_called: int = 0
        self.stop_called: int = 0

    def predict(self, pcm_data: bytes) -> float | dict[str, float]:
        self.calls.append(pcm_data)
        if self.fail_with_exception is not None:
            raise self.fail_with_exception
        if self.return_dict is not None:
            return self.return_dict
        return self.return_score

    def reset(self) -> None:
        self.reset_called += 1

    def stop(self) -> None:
        self.stop_called += 1


class FakeAudioCaptureService:
    """Mock AudioCaptureService that yields controllable PCM frames."""

    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self._frame_queue: queue.Queue[bytes] = queue.Queue()
        self.is_running: bool = True

    def feed_frame(self, frame: bytes) -> None:
        self._frame_queue.put(frame)

    def read_frame(self, timeout: float = 0.1) -> bytes | None:
        try:
            return self._frame_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None


@pytest.fixture
def base_config() -> VoiceConfig:
    """Standard test configuration with fast cooldown and small buffers."""
    return VoiceConfig(
        sample_rate=16000,
        channels=1,
        sample_width=2,
        frame_duration_ms=20,
        wake_word_phrase="Hey Friday",
        wake_word_threshold=0.5,
        wake_word_cooldown_seconds=1.0,
        wake_word_buffer_duration_sec=2.0,
    )


# ==============================================================================
# 1. Successful Wake-Word Detection
# ==============================================================================

def test_successful_wake_word_detection(base_config):
    """Verify that an engine score exceeding threshold triggers successful detection."""
    engine = MockWakeWordEngine(return_score=0.85)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples
    t_before = time.time()
    result = detector.process_frame(frame)
    t_after = time.time()

    assert result.detected is True
    assert result.wake_phrase == "Hey Friday"
    assert result.confidence == 0.85
    assert t_before <= result.timestamp <= t_after
    assert result.error is None
    assert len(engine.calls) == 1


# ==============================================================================
# 2. No Wake-Word Detection
# ==============================================================================

def test_no_wake_word_detection(base_config):
    """Verify that an engine score below threshold does not trigger detection."""
    engine = MockWakeWordEngine(return_score=0.25)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples
    result = detector.process_frame(frame)

    assert result.detected is False
    assert result.wake_phrase == "Hey Friday"
    assert result.confidence == 0.25
    assert result.error is None


# ==============================================================================
# 3. Different Configured Wake Phrases
# ==============================================================================

def test_different_configured_wake_phrases():
    """Verify that detector respects custom wake phrases in config and dictionary results."""
    # Test "Hey Jarvis"
    config_jarvis = VoiceConfig(wake_word_phrase="Hey Jarvis", wake_word_threshold=0.6)
    engine_jarvis = MockWakeWordEngine()
    engine_jarvis.return_dict = {"hey_jarvis": 0.88, "hey_friday": 0.12}

    detector_jarvis = WakeWordDetector(config=config_jarvis, engine=engine_jarvis)
    frame = b"\x00\x00" * config_jarvis.frame_samples
    result_jarvis = detector_jarvis.process_frame(frame)

    assert result_jarvis.detected is True
    assert result_jarvis.wake_phrase == "Hey Jarvis"
    assert result_jarvis.confidence == 0.88

    # Test "Computer"
    config_computer = VoiceConfig(wake_word_phrase="Computer", wake_word_threshold=0.7)
    engine_computer = MockWakeWordEngine()
    engine_computer.return_dict = {"computer": 0.95}

    detector_computer = WakeWordDetector(config=config_computer, engine=engine_computer)
    result_computer = detector_computer.process_frame(frame)

    assert result_computer.detected is True
    assert result_computer.wake_phrase == "Computer"
    assert result_computer.confidence == 0.95


# ==============================================================================
# 4. Detection Cooldown
# ==============================================================================

def test_detection_cooldown(base_config):
    """Verify that detections are suppressed during the configured cooldown period."""
    engine = MockWakeWordEngine(return_score=0.90)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    # First frame triggers detection
    result1 = detector.process_frame(frame)
    assert result1.detected is True
    assert detector.last_detection_timestamp > 0.0

    # Second frame immediately arrives within cooldown (cooldown is 1.0s)
    result2 = detector.process_frame(frame)
    assert result2.detected is False
    assert result2.confidence == 0.0
    # Engine was not called a second time because cooldown intercepted it early
    assert len(engine.calls) == 1


# ==============================================================================
# 5. Duplicate Detection Prevention
# ==============================================================================

def test_duplicate_detection_prevention(base_config):
    """Verify multiple incoming frames during activation do not trigger duplicate events."""
    engine = MockWakeWordEngine(return_score=0.95)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    # Trigger detection
    res_first = detector.process_frame(frame)
    assert res_first.detected is True

    # Feed 10 consecutive frames rapidly within cooldown
    for _ in range(10):
        res = detector.process_frame(frame)
        assert res.detected is False

    assert len(engine.calls) == 1


# ==============================================================================
# 6. Invalid Audio Frames
# ==============================================================================

def test_invalid_audio_frames(base_config):
    """Verify non-bytes, empty, and misaligned frames return safe errors without crashing."""
    detector = WakeWordDetector(config=base_config, engine=MockWakeWordEngine())

    # 1. Non-bytes input
    res_str = detector.process_frame("not_bytes")  # type: ignore[arg-type]
    assert res_str.detected is False
    assert "Audio frame must be bytes" in res_str.error

    res_int = detector.process_frame(12345)  # type: ignore[arg-type]
    assert res_int.detected is False
    assert "Audio frame must be bytes" in res_int.error

    res_none = detector.process_frame(None)  # type: ignore[arg-type]
    assert res_none.detected is False
    assert "Audio frame must be bytes" in res_none.error

    # 2. Empty bytes
    res_empty = detector.process_frame(b"")
    assert res_empty.detected is False
    assert "Audio frame is empty" in res_empty.error

    # 3. Unaligned PCM bytes (15 bytes when 16-bit PCM requires even count)
    res_unaligned = detector.process_frame(b"\x00" * 15)
    assert res_unaligned.detected is False
    assert "not aligned to sample width" in res_unaligned.error


# ==============================================================================
# 7. Unsupported Audio Format Validation
# ==============================================================================

def test_unsupported_audio_format_validation():
    """Verify VoiceConfig rejects unsupported sample rates, channels, or sample widths."""
    with pytest.raises(ValueError, match="Unsupported sample_rate"):
        VoiceConfig(sample_rate=22050)

    with pytest.raises(ValueError, match="Only mono audio"):
        VoiceConfig(channels=2)

    with pytest.raises(ValueError, match="Only 16-bit linear PCM"):
        VoiceConfig(sample_width=4)


# ==============================================================================
# 8. Missing Model File
# ==============================================================================

def test_missing_model_file():
    """Verify detector reports unavailable when model path does not exist on disk."""
    config = VoiceConfig(
        wake_word_model_path="C:/nonexistent/path/to/hey_friday.onnx",
        wake_word_engine="openwakeword",
    )
    detector = WakeWordDetector(config=config)

    assert detector.is_available() is False
    frame = b"\x00\x00" * config.frame_samples
    res = detector.process_frame(frame)
    assert res.detected is False
    assert "unavailable" in res.error.lower()


# ==============================================================================
# 9. Unavailable Detection Engine
# ==============================================================================

def test_unavailable_detection_engine():
    """Verify detector handles unconfigured / unavailable engine cleanly."""
    config = VoiceConfig(wake_word_model_path=None, wake_word_engine="openwakeword")
    detector = WakeWordDetector(config=config)

    assert detector.is_available() is False
    frame = b"\x00\x00" * config.frame_samples
    res = detector.process_frame(frame)
    assert res.detected is False
    assert "unavailable" in res.error.lower()


# ==============================================================================
# 10. Detection Engine Exception
# ==============================================================================

def test_detection_engine_exception(base_config):
    """Verify detector catches engine exceptions safely and returns descriptive error."""
    engine = MockWakeWordEngine()
    engine.fail_with_exception = RuntimeError("Simulated ONNX Runtime inference memory error")
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples
    res = detector.process_frame(frame)

    assert res.detected is False
    assert "Detection engine error" in res.error
    assert "RuntimeError" in res.error
    assert detector.is_active is True


# ==============================================================================
# 11. Reset After Detection
# ==============================================================================

def test_reset_after_detection(base_config):
    """Verify that reset() clears cooldown and buffer, allowing immediate re-activation."""
    engine = MockWakeWordEngine(return_score=0.9)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    # Trigger first detection
    res1 = detector.process_frame(frame)
    assert res1.detected is True
    assert detector.last_detection_timestamp > 0.0

    # Reset
    detector.reset()
    assert detector.last_detection_timestamp == 0.0
    assert engine.reset_called == 1
    assert len(detector._audio_buffer) == 0

    # Next frame should immediately be allowed to trigger detection
    res2 = detector.process_frame(frame)
    assert res2.detected is True


# ==============================================================================
# 12. Shutdown and Resource Cleanup
# ==============================================================================

def test_shutdown_and_resource_cleanup(base_config):
    """Verify stop() releases resources and rejects subsequent processing."""
    engine = MockWakeWordEngine(return_score=0.8)
    detector = WakeWordDetector(config=base_config, engine=engine)

    assert detector.is_active is True
    assert detector.is_available() is True

    # Stop detector
    detector.stop()
    assert detector.is_active is False
    assert detector.is_available() is False
    assert engine.stop_called == 1

    # Processing while stopped
    frame = b"\x00\x00" * base_config.frame_samples
    res = detector.process_frame(frame)
    assert res.detected is False
    assert "stopped" in res.error.lower()

    # Repeated stop is idempotent
    detector.stop()
    assert engine.stop_called == 1


# ==============================================================================
# 13. Repeated Detection Attempts
# ==============================================================================

def test_repeated_detection_attempts(base_config):
    """Verify detector processes a stream of 25 frames consistently across transitions."""
    engine = MockWakeWordEngine(return_score=0.2)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    for _ in range(25):
        res = detector.process_frame(frame)
        assert res.detected is False
        assert res.error is None

    assert len(engine.calls) == 25


# ==============================================================================
# 14. Bounded Audio Buffering
# ==============================================================================

def test_bounded_audio_buffering():
    """Verify internal buffer never exceeds wake_word_buffer_duration_sec."""
    # 0.5 sec buffer = 16000 * 2 * 0.5 = 16000 bytes
    config = VoiceConfig(wake_word_buffer_duration_sec=0.5)
    engine = MockWakeWordEngine(return_score=0.1)
    detector = WakeWordDetector(config=config, engine=engine)

    max_expected_bytes = int(config.sample_rate * config.bytes_per_sample * 0.5)
    frame = b"\x00\x00" * config.frame_samples  # 640 bytes

    # Feed 100 frames = 64,000 bytes
    for _ in range(100):
        detector.process_frame(frame)
        assert len(detector._audio_buffer) <= max_expected_bytes

    assert len(detector._audio_buffer) == max_expected_bytes


# ==============================================================================
# 15. Correct Sample Rate and Frame Size Handling
# ==============================================================================

def test_sample_rate_and_frame_size_handling(base_config):
    """Verify 16kHz, 20ms frames match expected byte count (640 bytes)."""
    assert base_config.sample_rate == 16000
    assert base_config.frame_duration_ms == 20
    assert base_config.frame_samples == 320
    assert base_config.frame_bytes == 640

    engine = MockWakeWordEngine(return_score=0.1)
    detector = WakeWordDetector(config=base_config, engine=engine)

    valid_frame = b"\x01\x00" * 320
    assert len(valid_frame) == 640

    res = detector.process_frame(valid_frame)
    assert res.error is None
    assert len(engine.calls[0]) == 640


# ==============================================================================
# 16. Configuration Validation
# ==============================================================================

def test_configuration_validation():
    """Verify strict bound validation on wake-word configuration settings."""
    # Empty phrase
    with pytest.raises(ValueError, match="wake_word_phrase cannot be empty"):
        VoiceConfig(wake_word_phrase="   ")

    # Threshold bounds
    with pytest.raises(ValueError, match="wake_word_threshold must be between 0.0 and 1.0"):
        VoiceConfig(wake_word_threshold=-0.1)

    with pytest.raises(ValueError, match="wake_word_threshold must be between 0.0 and 1.0"):
        VoiceConfig(wake_word_threshold=1.5)

    # Cooldown bounds
    with pytest.raises(ValueError, match="wake_word_cooldown_seconds cannot be negative"):
        VoiceConfig(wake_word_cooldown_seconds=-1.0)

    # Buffer duration bounds
    with pytest.raises(ValueError, match="wake_word_buffer_duration_sec must be positive"):
        VoiceConfig(wake_word_buffer_duration_sec=0.0)

    # Engine name bounds
    with pytest.raises(ValueError, match="Unsupported wake_word_engine"):
        VoiceConfig(wake_word_engine="invalid_engine")


# ==============================================================================
# 17. No Cloud-Service Dependency
# ==============================================================================

def test_no_cloud_service_dependency(base_config):
    """Verify detector functions completely offline with zero network connections."""
    engine = MockWakeWordEngine(return_score=0.88)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    # Patch socket.socket.connect to raise if any network call is attempted
    with patch("socket.socket.connect") as mock_connect:
        mock_connect.side_effect = RuntimeError("Network call attempted during local wake-word detection!")

        res = detector.process_frame(frame)
        assert res.detected is True
        assert res.confidence == 0.88
        assert mock_connect.call_count == 0


# ==============================================================================
# 18. No Whisper Invocation During Continuous Wake-Word Monitoring
# ==============================================================================

def test_no_whisper_invocation_during_wake_word_monitoring(base_config):
    """Verify wake-word detection never calls Whisper transcription."""
    engine = MockWakeWordEngine(return_score=0.85)
    detector = WakeWordDetector(config=base_config, engine=engine)

    frame = b"\x00\x00" * base_config.frame_samples

    # Spy on SpeechToTextService.transcribe
    with patch.object(SpeechToTextService, "transcribe") as mock_whisper:
        for _ in range(5):
            res = detector.process_frame(frame)
            if res.detected:
                detector.reset()

        assert mock_whisper.call_count == 0


# ==============================================================================
# 19. Continuous Listening with AudioCaptureService
# ==============================================================================

def test_listen_for_wake_word_with_capture_service(base_config):
    """Verify listen_for_wake_word reads from capture service until detection occurs."""
    engine = MockWakeWordEngine(return_score=0.0)
    detector = WakeWordDetector(config=base_config, engine=engine)
    capture = FakeAudioCaptureService(base_config)

    frame = b"\x00\x00" * base_config.frame_samples

    # Feed 2 frames with score 0.0
    capture.feed_frame(frame)
    capture.feed_frame(frame)

    # Background feeder thread: after 0.05s, set engine score to 0.9 and feed frame
    def delayed_feed():
        time.sleep(0.05)
        engine.return_score = 0.9
        capture.feed_frame(frame)

    t = threading.Thread(target=delayed_feed)
    t.start()

    res = detector.listen_for_wake_word(capture, timeout=0.5)
    t.join()

    assert res is not None
    assert res.detected is True
    assert res.wake_phrase == "Hey Friday"
    assert res.confidence == 0.9


def test_listen_for_wake_word_timeout_returns_none(base_config):
    """Verify listen_for_wake_word returns None when timeout expires without detection."""
    engine = MockWakeWordEngine(return_score=0.1)
    detector = WakeWordDetector(config=base_config, engine=engine)
    capture = FakeAudioCaptureService(base_config)

    # Frame queue is empty, reading returns None
    res = detector.listen_for_wake_word(capture, timeout=0.1)
    assert res is None
