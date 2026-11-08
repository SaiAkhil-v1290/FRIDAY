"""
Unit and integration tests for JARVIS Local Wake-Word Detection Subsystem (Milestone 5.1.4 & 5.1.4.1).
Validates openWakeWord Model interface contract, int16 NumPy array conversion, 80ms chunk aggregation,
model key identity matching, cooldown handling, bounded memory, and offline execution without Whisper or cloud calls.
"""

from __future__ import annotations

import queue
import socket
import sys
import threading
import time
from unittest.mock import MagicMock, patch
import numpy as np
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
    OPENWAKEWORD_CHUNK_BYTES,
    OPENWAKEWORD_CHUNK_SAMPLES,
)


class RealStyleOpenWakeWordModelMock:
    """
    Simulates the actual production openWakeWord Model interface.
    Enforces that predict() receives a signed int16 NumPy array of 1,280 samples (80ms),
    and returns a dictionary mapping model names to confidence scores.
    """

    def __init__(self, model_key: str = "hey_friday", return_score: float = 0.0) -> None:
        self.model_key = model_key
        self.return_score = return_score
        self.models = {model_key: MagicMock()}
        self.received_arrays: list[np.ndarray] = []
        self.reset_called: int = 0
        self.stop_called: int = 0
        self.fail_on_predict: bool = False

    def predict(self, x: np.ndarray) -> dict[str, float]:
        if not isinstance(x, np.ndarray):
            raise TypeError(f"openWakeWord expects np.ndarray, got {type(x).__name__}")
        if x.dtype != np.int16:
            raise TypeError(f"openWakeWord expects int16 dtype, got {x.dtype}")
        self.received_arrays.append(x)
        if self.fail_on_predict:
            raise RuntimeError("Simulated openWakeWord ONNX inference error")
        return {self.model_key: self.return_score}

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
    """Standard test configuration with 20ms frames and 80ms chunk aggregation."""
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
# 1. Real Adapter Audio Interface: int16 NumPy Array Conversion
# ==============================================================================

def test_predict_receives_int16_numpy_array(base_config):
    """Verify that openWakeWord predict() receives an int16 NumPy array of exactly 1,280 samples."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.85)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    # 4 frames of 20ms (640 bytes each) = 2,560 bytes = 1,280 samples
    pcm_20ms = b"\x01\x00" * 320  # 320 samples of int16 value 1
    for _ in range(4):
        detector.process_frame(pcm_20ms)

    assert len(mock_model.received_arrays) == 1
    arr = mock_model.received_arrays[0]

    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.int16
    assert len(arr) == OPENWAKEWORD_CHUNK_SAMPLES  # 1280
    assert arr.nbytes == OPENWAKEWORD_CHUNK_BYTES  # 2560
    assert np.all(arr == 1)


def test_pcm_to_numpy_conversion_preserves_values(base_config):
    """Verify conversion accurately preserves signed int16 waveform samples without alteration."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.8)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    # Create signed int16 test pattern: -32768, -100, 0, 100, 32767
    sample_pattern = np.array([-32768, -100, 0, 100, 32767], dtype=np.int16)
    full_samples = np.tile(sample_pattern, 1280 // len(sample_pattern))
    full_pcm = full_samples.tobytes()

    # Feed in two 1280-byte halves (representing two 40ms blocks or four 20ms frames)
    detector.process_frame(full_pcm[:1280])
    detector.process_frame(full_pcm[1280:])

    assert len(mock_model.received_arrays) == 1
    received = mock_model.received_arrays[0]
    np.testing.assert_array_equal(received, full_samples)


# ==============================================================================
# 2. Audio Frame Aggregation: Four 20ms Frames produce One 80ms Inference
# ==============================================================================

def test_four_20ms_frames_produce_one_80ms_inference(base_config):
    """Verify that no inference occurs before 80ms (4 frames), and exactly one occurs at 80ms."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.9)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples  # 640 bytes (20ms)

    # Frame 1: 20ms (640 bytes) -> No inference
    r1 = detector.process_frame(frame)
    assert r1.detected is False
    assert len(mock_model.received_arrays) == 0

    # Frame 2: 40ms (1280 bytes) -> No inference
    r2 = detector.process_frame(frame)
    assert r2.detected is False
    assert len(mock_model.received_arrays) == 0

    # Frame 3: 60ms (1920 bytes) -> No inference
    r3 = detector.process_frame(frame)
    assert r3.detected is False
    assert len(mock_model.received_arrays) == 0

    # Frame 4: 80ms (2560 bytes) -> Exactly ONE inference call
    r4 = detector.process_frame(frame)
    assert r4.detected is True
    assert r4.confidence == 0.9
    assert len(mock_model.received_arrays) == 1


def test_consecutive_chunks_processed_without_sample_duplication(base_config):
    """Verify consecutive 80ms chunks process sequentially without overlapping or duplicating samples."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.1)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    # Chunk 1: all 1s (4 frames)
    frame_ones = (np.ones(320, dtype=np.int16)).tobytes()
    for _ in range(4):
        detector.process_frame(frame_ones)

    # Chunk 2: all 2s (4 frames)
    frame_twos = (np.full(320, 2, dtype=np.int16)).tobytes()
    for _ in range(4):
        detector.process_frame(frame_twos)

    assert len(mock_model.received_arrays) == 2
    np.testing.assert_array_equal(mock_model.received_arrays[0], np.ones(1280, dtype=np.int16))
    np.testing.assert_array_equal(mock_model.received_arrays[1], np.full(1280, 2, dtype=np.int16))


# ==============================================================================
# 3. Model and Phrase Mapping: Identity Enforcement
# ==============================================================================

def test_model_output_matches_configured_key(base_config):
    """Verify that output key matching the configured wake phrase triggers detection."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.88)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    for _ in range(3):
        detector.process_frame(frame)
    res = detector.process_frame(frame)

    assert res.detected is True
    assert res.wake_phrase == "Hey Friday"
    assert res.confidence == 0.88
    assert res.error is None


def test_unrelated_model_key_cannot_activate_configured_phrase(base_config):
    """Verify that a high score from an unrelated model key (e.g. alexa) NEVER activates Hey Friday."""
    # Engine returns high score (0.99) for "alexa", but detector expects "Hey Friday"
    mock_model = RealStyleOpenWakeWordModelMock(model_key="alexa", return_score=0.99)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    for _ in range(3):
        detector.process_frame(frame)
    res = detector.process_frame(frame)

    assert res.detected is False
    assert res.confidence == 0.0
    assert res.error is not None
    assert "Model output mismatch" in res.error
    assert "alexa" in res.error


def test_explicit_model_key_configuration():
    """Verify that wake_word_model_key allows matching custom model key names."""
    custom_config = VoiceConfig(
        wake_word_phrase="Hey Friday",
        wake_word_model_key="hey_friday_v0.1",
        wake_word_threshold=0.6,
    )
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday_v0.1", return_score=0.82)
    detector = WakeWordDetector(config=custom_config, engine=mock_model)

    frame = b"\x00\x00" * custom_config.frame_samples
    for _ in range(3):
        detector.process_frame(frame)
    res = detector.process_frame(frame)

    assert res.detected is True
    assert res.confidence == 0.82
    assert res.error is None


# ==============================================================================
# 4. Availability and Initialization
# ==============================================================================

def test_missing_model_file_makes_is_available_false():
    """Verify is_available() returns False and records error when model file does not exist."""
    config = VoiceConfig(
        wake_word_model_path="C:/nonexistent/dir/hey_friday.onnx",
        wake_word_engine="openwakeword",
    )
    detector = WakeWordDetector(config=config)

    assert detector.is_available() is False
    assert detector.initialization_error is not None
    assert "not found on disk" in detector.initialization_error

    # Attempting to process frame returns clear error without crashing
    frame = b"\x00\x00" * config.frame_samples
    res = detector.process_frame(frame)
    assert res.detected is False
    assert "not found on disk" in res.error


def test_unconfigured_model_path_makes_is_available_false():
    """Verify is_available() returns False when wake_word_model_path is None."""
    config = VoiceConfig(wake_word_model_path=None, wake_word_engine="openwakeword")
    detector = WakeWordDetector(config=config)

    assert detector.is_available() is False
    assert detector.initialization_error == "No wake_word_model_path configured."


def test_missing_package_makes_is_available_false(tmp_path):
    """Verify is_available() returns False when openwakeword package is not installed."""
    dummy_model = tmp_path / "model.onnx"
    dummy_model.write_bytes(b"dummy")

    config = VoiceConfig(
        wake_word_model_path=str(dummy_model),
        wake_word_engine="openwakeword",
    )

    with patch.dict(sys.modules, {"openwakeword": None, "openwakeword.model": None}):
        detector = WakeWordDetector(config=config)
        assert detector.is_available() is False
        assert "not installed" in detector.initialization_error.lower()


def test_initialization_exception_makes_is_available_false(tmp_path):
    """Verify is_available() returns False when Model initialization raises an exception."""
    dummy_model = tmp_path / "corrupt.onnx"
    dummy_model.write_bytes(b"corrupt")

    config = VoiceConfig(
        wake_word_model_path=str(dummy_model),
        wake_word_engine="openwakeword",
    )

    mock_model_module = MagicMock()
    mock_model_module.Model.side_effect = RuntimeError("ONNX Runtime graph parsing failed")

    with patch.dict(sys.modules, {
        "openwakeword": MagicMock(),
        "openwakeword.model": mock_model_module,
    }):
        detector = WakeWordDetector(config=config)
        assert detector.is_available() is False
        assert "Failed to initialize openWakeWord ONNX engine" in detector.initialization_error
        assert "RuntimeError" in detector.initialization_error


# ==============================================================================
# 5. Engine Configuration and Framework Validation
# ==============================================================================

def test_inference_framework_configuration_validation():
    """Verify that wake_word_inference_framework must be 'onnx'."""
    # Valid onnx configuration
    valid_cfg = VoiceConfig(wake_word_inference_framework="onnx")
    assert valid_cfg.wake_word_inference_framework == "onnx"

    # Unsupported tflite or custom framework raises ValueError
    with pytest.raises(ValueError, match="Unsupported wake_word_inference_framework"):
        VoiceConfig(wake_word_inference_framework="tflite")

    with pytest.raises(ValueError, match="Unsupported wake_word_inference_framework"):
        VoiceConfig(wake_word_inference_framework="coreml")


# ==============================================================================
# 6. Model Reset, Stop, and Resource Cleanup
# ==============================================================================

def test_model_reset_clears_model_state_and_partial_buffer(base_config):
    """Verify reset() clears cooldown, partial audio buffer, and calls engine reset."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.9)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples

    # Feed 2 frames (40ms, incomplete chunk)
    detector.process_frame(frame)
    detector.process_frame(frame)
    assert len(detector._audio_buffer) == 1280

    # Reset
    detector.reset()
    assert len(detector._audio_buffer) == 0
    assert mock_model.reset_called == 1
    assert detector.last_detection_timestamp == 0.0

    # Now feeding 2 frames still does not trigger inference because buffer was cleared
    detector.process_frame(frame)
    detector.process_frame(frame)
    assert len(mock_model.received_arrays) == 0


def test_stop_releases_engine_and_clears_buffer(base_config):
    """Verify stop() clears buffer, calls engine stop, and sets detector inactive."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.8)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    detector.process_frame(frame)

    detector.stop()
    assert detector.is_active is False
    assert detector.is_available() is False
    assert len(detector._audio_buffer) == 0
    assert mock_model.stop_called == 1

    # Processing while stopped returns error
    res = detector.process_frame(frame)
    assert res.detected is False
    assert "stopped" in res.error.lower()

    # Repeated stop is idempotent
    detector.stop()
    assert mock_model.stop_called == 1


# ==============================================================================
# 7. Cooldown and Duplicate Detection Prevention
# ==============================================================================

def test_detection_cooldown_and_duplicate_suppression(base_config):
    """Verify cooldown suppresses repeated detections from incoming chunks."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.95)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples

    # 4 frames trigger detection
    for _ in range(3):
        detector.process_frame(frame)
    res1 = detector.process_frame(frame)
    assert res1.detected is True
    assert len(mock_model.received_arrays) == 1

    # Feed next 4 frames immediately within cooldown (cooldown=1.0s)
    for _ in range(3):
        detector.process_frame(frame)
    res2 = detector.process_frame(frame)
    assert res2.detected is False
    assert res2.confidence == 0.0
    # Engine was NOT called because cooldown intercepted the chunk early
    assert len(mock_model.received_arrays) == 1


# ==============================================================================
# 8. Bounded Audio Buffering
# ==============================================================================

def test_buffer_memory_remains_bounded(base_config):
    """Verify that internal audio buffer remains strictly bounded under continuous frame input."""
    mock_model = RealStyleOpenWakeWordModelMock(model_key="hey_friday", return_score=0.1)
    # 1.0 second buffer limit = 32,000 bytes
    config = VoiceConfig(wake_word_buffer_duration_sec=1.0)
    detector = WakeWordDetector(config=config, engine=mock_model)

    frame = b"\x00\x00" * config.frame_samples  # 640 bytes

    # Feed 100 frames (64,000 bytes)
    for _ in range(100):
        detector.process_frame(frame)
        # Because 80ms chunks are extracted, the remainder never exceeds 2,560 bytes
        assert len(detector._audio_buffer) < OPENWAKEWORD_CHUNK_BYTES

    assert len(detector._audio_buffer) >= 0


# ==============================================================================
# 9. Invalid Audio Frames & Exceptions
# ==============================================================================

def test_invalid_audio_frames(base_config):
    """Verify non-bytes, empty, and misaligned frames return errors without crashing."""
    mock_model = RealStyleOpenWakeWordModelMock()
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    assert detector.process_frame("str").detected is False  # type: ignore[arg-type]
    assert detector.process_frame(b"").detected is False
    assert detector.process_frame(b"\x00" * 15).detected is False  # unaligned


def test_engine_exception_handled_safely(base_config):
    """Verify that an exception raised by predict() is caught safely and reports error."""
    mock_model = RealStyleOpenWakeWordModelMock(return_score=0.8)
    mock_model.fail_on_predict = True
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    for _ in range(3):
        detector.process_frame(frame)
    res = detector.process_frame(frame)

    assert res.detected is False
    assert "Detection engine error" in res.error
    assert "RuntimeError" in res.error
    assert detector.is_active is True


# ==============================================================================
# 10. Privacy and Isolation: Zero Cloud, Zero Whisper
# ==============================================================================

def test_no_cloud_dependencies(base_config):
    """Verify wake-word detection makes zero network socket calls."""
    mock_model = RealStyleOpenWakeWordModelMock(return_score=0.8)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    with patch("socket.socket.connect") as mock_connect:
        mock_connect.side_effect = RuntimeError("Network access attempted!")
        for _ in range(4):
            detector.process_frame(frame)
        assert mock_connect.call_count == 0


def test_no_whisper_invocation_during_monitoring(base_config):
    """Verify wake-word detector never invokes Whisper transcription."""
    mock_model = RealStyleOpenWakeWordModelMock(return_score=0.85)
    detector = WakeWordDetector(config=base_config, engine=mock_model)

    frame = b"\x00\x00" * base_config.frame_samples
    with patch.object(SpeechToTextService, "transcribe") as mock_stt:
        for _ in range(8):
            res = detector.process_frame(frame)
            if res.detected:
                detector.reset()
        assert mock_stt.call_count == 0


# ==============================================================================
# 11. Continuous Listening Integration with AudioCaptureService
# ==============================================================================

def test_listen_for_wake_word_integration(base_config):
    """Verify listen_for_wake_word reads 20ms frames until 80ms chunk triggers detection."""
    mock_model = RealStyleOpenWakeWordModelMock(return_score=0.0)
    detector = WakeWordDetector(config=base_config, engine=mock_model)
    capture = FakeAudioCaptureService(base_config)

    frame = b"\x00\x00" * base_config.frame_samples

    # Feed 3 frames (60ms) -> no detection yet
    for _ in range(3):
        capture.feed_frame(frame)

    def feeder():
        time.sleep(0.02)
        mock_model.return_score = 0.95
        # 4th frame arrives (80ms reached)
        capture.feed_frame(frame)

    t = threading.Thread(target=feeder)
    t.start()

    res = detector.listen_for_wake_word(capture, timeout=0.5)
    t.join()

    assert res is not None
    assert res.detected is True
    assert res.wake_phrase == "Hey Friday"
    assert res.confidence == 0.95
