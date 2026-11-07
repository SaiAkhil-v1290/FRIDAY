"""
Unit and integration tests for JARVIS Voice Audio I/O Subsystem (Milestone 5.1.1).
Tests audio capture, playback, bounded buffers, lifecycle safety, and device error handling
using isolated mock sources and sinks without requiring physical microphones or speakers.
"""

import time
import threading
import unittest
from unittest.mock import patch, MagicMock

from voice.config import VoiceConfig
from voice.audio_io import (
    AudioInputSource,
    AudioOutputSink,
    AudioCaptureService,
    AudioPlaybackService,
    SoundDeviceInputSource,
    SoundDeviceOutputSink,
    AudioDeviceError,
    AudioConfigError,
    AudioReadError,
    AudioWriteError,
)


class MockAudioInputSource(AudioInputSource):
    """Controllable mock audio input source for testing."""

    def __init__(self, frame_bytes: int = 640, fail_on_open: bool = False) -> None:
        self.frame_bytes = frame_bytes
        self.fail_on_open = fail_on_open
        self._active = False
        self.read_call_count = 0
        self.fail_on_read_count: int | None = None
        self.close_call_count = 0

    def open(self) -> None:
        if self.fail_on_open:
            raise AudioDeviceError("Simulated missing or busy microphone.")
        self._active = True

    def read(self, num_samples: int) -> bytes:
        if not self._active:
            raise AudioReadError("Cannot read from inactive source.")

        self.read_call_count += 1
        if self.fail_on_read_count is not None and self.read_call_count >= self.fail_on_read_count:
            self._active = False
            raise AudioDeviceError("Simulated microphone disconnection.")

        # Simulate real-time audio chunk pacing
        time.sleep(0.005)
        return b"\x01\x00" * num_samples

    def close(self) -> None:
        self._active = False
        self.close_call_count += 1

    @property
    def is_active(self) -> bool:
        return self._active


class MockAudioOutputSink(AudioOutputSink):
    """Controllable mock audio output sink for testing."""

    def __init__(self, fail_on_open: bool = False, fail_on_write: bool = False) -> None:
        self.fail_on_open = fail_on_open
        self.fail_on_write = fail_on_write
        self._active = False
        self.written_chunks: list[bytes] = []
        self.stop_called = False
        self.close_called = False

    def open(self) -> None:
        if self.fail_on_open:
            raise AudioDeviceError("Simulated missing speaker device.")
        self._active = True

    def write(self, pcm_data: bytes) -> None:
        if not self._active:
            raise AudioWriteError("Cannot write to inactive sink.")
        if self.fail_on_write:
            self._active = False
            raise AudioDeviceError("Simulated audio output failure.")

        self.written_chunks.append(pcm_data)
        time.sleep(0.005)

    def stop(self) -> None:
        self.stop_called = True

    def close(self) -> None:
        self._active = False
        self.close_called = True

    @property
    def is_active(self) -> bool:
        return self._active


class TestAudioConfig(unittest.TestCase):
    """Tests for VoiceConfig validation and frame calculations."""

    def test_valid_default_config(self):
        cfg = VoiceConfig()
        self.assertEqual(cfg.sample_rate, 16000)
        self.assertEqual(cfg.channels, 1)
        self.assertEqual(cfg.sample_width, 2)
        self.assertEqual(cfg.frame_duration_ms, 20)
        self.assertEqual(cfg.frame_samples, 320)
        self.assertEqual(cfg.frame_bytes, 640)
        self.assertEqual(cfg.bytes_per_sample, 2)

    def test_invalid_sample_rate(self):
        with self.assertRaises(ValueError):
            VoiceConfig(sample_rate=44100)  # webrtcvad requires 8000, 16000, 32000, or 48000

    def test_invalid_channels(self):
        with self.assertRaises(ValueError):
            VoiceConfig(channels=2)

    def test_invalid_sample_width(self):
        with self.assertRaises(ValueError):
            VoiceConfig(sample_width=4)

    def test_invalid_frame_duration(self):
        with self.assertRaises(ValueError):
            VoiceConfig(frame_duration_ms=50)

    def test_invalid_vad_mode(self):
        with self.assertRaises(ValueError):
            VoiceConfig(vad_mode=5)


class TestAudioCaptureService(unittest.TestCase):
    """Tests for AudioCaptureService lifecycle, bounded buffers, and error recovery."""

    def setUp(self):
        self.config = VoiceConfig(frame_duration_ms=10)  # 10ms = 160 samples = 320 bytes
        self.mock_source = MockAudioInputSource(frame_bytes=self.config.frame_bytes)
        self.capture_service = AudioCaptureService(source=self.mock_source, config=self.config)

    def tearDown(self):
        self.capture_service.shutdown()

    def test_capture_lifecycle_success(self):
        self.assertFalse(self.capture_service.is_running)
        self.capture_service.start()
        self.assertTrue(self.capture_service.is_running)
        self.assertTrue(self.mock_source.is_active)

        # Read captured frames
        frame = self.capture_service.read_frame(timeout=0.5)
        self.assertIsNotNone(frame)
        self.assertEqual(len(frame), self.config.frame_bytes)

        self.capture_service.stop()
        self.assertFalse(self.capture_service.is_running)
        self.assertFalse(self.mock_source.is_active)
        self.assertEqual(self.mock_source.close_call_count, 1)

    def test_missing_microphone_error(self):
        failing_source = MockAudioInputSource(fail_on_open=True)
        service = AudioCaptureService(source=failing_source, config=self.config)
        with self.assertRaises(AudioDeviceError):
            service.start()
        self.assertFalse(service.is_running)

    def test_microphone_disconnection_during_capture(self):
        error_recorded = []

        def handle_error(exc):
            error_recorded.append(exc)

        self.mock_source.fail_on_read_count = 3
        service = AudioCaptureService(
            source=self.mock_source, config=self.config, on_error=handle_error
        )
        service.start()

        # Wait for failure to occur in background thread
        time.sleep(0.1)
        self.assertFalse(service.is_running)
        self.assertIsNotNone(service.last_error)
        self.assertTrue(len(error_recorded) > 0)
        self.assertIn("Simulated microphone disconnection", str(service.last_error))
        service.stop()

    def test_bounded_queue_overflow_handling(self):
        # Configure small queue size of 5 frames
        small_queue_cfg = VoiceConfig(frame_duration_ms=10, max_queue_size=5)
        service = AudioCaptureService(source=self.mock_source, config=small_queue_cfg)
        service.start()

        # Allow worker to produce > 5 frames without draining
        time.sleep(0.08)
        service.stop()

        # Queue should be bounded to 5 items, overflow tracked
        self.assertLessEqual(service._queue.qsize(), 5)
        self.assertGreaterEqual(service.overflow_count, 0)

    def test_repeated_start_stop_cycles(self):
        for cycle in range(3):
            self.capture_service.start()
            self.assertTrue(self.capture_service.is_running)
            frame = self.capture_service.read_frame(timeout=0.2)
            self.assertIsNotNone(frame)
            self.capture_service.stop()
            self.assertFalse(self.capture_service.is_running)

    def test_no_duplicate_workers(self):
        self.capture_service.start()
        worker1 = self.capture_service._worker_thread
        # Calling start() again should be a no-op
        self.capture_service.start()
        worker2 = self.capture_service._worker_thread
        self.assertIs(worker1, worker2)
        self.capture_service.stop()

    def test_clear_buffer(self):
        self.capture_service.start()
        time.sleep(0.03)
        self.assertGreater(self.capture_service._queue.qsize(), 0)
        self.capture_service.clear_buffer()
        self.assertEqual(self.capture_service._queue.qsize(), 0)
        self.capture_service.stop()


class TestAudioPlaybackService(unittest.TestCase):
    """Tests for AudioPlaybackService playback, non-blocking flow, cancellation, and error handling."""

    def setUp(self):
        self.config = VoiceConfig(frame_duration_ms=10)
        self.mock_sink = MockAudioOutputSink()
        self.playback_service = AudioPlaybackService(sink=self.mock_sink, config=self.config)

    def tearDown(self):
        self.playback_service.shutdown()

    def test_blocking_playback_success(self):
        test_audio = b"\x02\x00" * 320  # 320 samples = 640 bytes
        self.playback_service.play(test_audio, non_blocking=False)
        self.assertFalse(self.playback_service.is_playing)
        self.assertTrue(len(self.mock_sink.written_chunks) > 0)
        total_written = sum(len(c) for c in self.mock_sink.written_chunks)
        self.assertEqual(total_written, len(test_audio))

    def test_non_blocking_playback(self):
        test_audio = b"\x03\x00" * 1600  # 1600 samples
        self.playback_service.play(test_audio, non_blocking=True)
        # Should start worker immediately
        time.sleep(0.01)
        self.assertTrue(self.playback_service.is_playing)

        # Wait for completion
        time.sleep(0.08)
        self.assertFalse(self.playback_service.is_playing)
        self.assertEqual(sum(len(c) for c in self.mock_sink.written_chunks), len(test_audio))

    def test_empty_audio_rejected(self):
        with self.assertRaises(ValueError):
            self.playback_service.play(b"")

    def test_unaligned_audio_rejected(self):
        with self.assertRaises(AudioConfigError):
            self.playback_service.play(b"\x01")  # Odd number of bytes for 16-bit PCM

    def test_missing_output_device_error(self):
        failing_sink = MockAudioOutputSink(fail_on_open=True)
        service = AudioPlaybackService(sink=failing_sink, config=self.config)
        test_audio = b"\x04\x00" * 160
        service.play(test_audio, non_blocking=False)
        self.assertIsNotNone(service.last_error)
        self.assertIn("Simulated missing speaker", str(service.last_error))

    def test_playback_immediate_stop_and_cancellation(self):
        test_audio = b"\x05\x00" * 3200  # Large audio
        self.playback_service.play(test_audio, non_blocking=True)
        time.sleep(0.01)
        self.assertTrue(self.playback_service.is_playing)

        # Immediate abort (Barge-in / Interruption)
        self.playback_service.stop()
        self.assertFalse(self.playback_service.is_playing)
        self.assertTrue(self.mock_sink.stop_called)
        # Written chunks should be less than the total audio data
        total_written = sum(len(c) for c in self.mock_sink.written_chunks)
        self.assertLess(total_written, len(test_audio))

    def test_duplicate_playback_prevents_overlap(self):
        audio1 = b"\x01\x00" * 2000
        audio2 = b"\x02\x00" * 500

        self.playback_service.play(audio1, non_blocking=True)
        time.sleep(0.01)
        self.assertTrue(self.playback_service.is_playing)

        # Trigger second playback: must abort audio1 immediately and play audio2
        self.playback_service.play(audio2, non_blocking=False)
        self.assertFalse(self.playback_service.is_playing)
        # Verify audio2 completed
        last_chunk = self.mock_sink.written_chunks[-1]
        self.assertEqual(last_chunk[:2], b"\x02\x00")

    def test_idempotent_shutdown(self):
        self.playback_service.shutdown()
        self.playback_service.shutdown()
        self.assertTrue(self.mock_sink.close_called)

    def test_reuse_after_playback_cancellation(self):
        """Verify that after playback is cancelled/stopped, subsequent playback succeeds cleanly."""
        long_audio = b"\x01\x00" * 3200
        self.playback_service.play(long_audio, non_blocking=True)
        time.sleep(0.01)
        self.assertTrue(self.playback_service.is_playing)

        # Cancel playback
        self.playback_service.stop()
        self.assertFalse(self.playback_service.is_playing)
        chunks_before = len(self.mock_sink.written_chunks)

        # Start a new playback using the same service and sink
        short_audio = b"\x02\x00" * 320
        self.playback_service.play(short_audio, non_blocking=False)
        self.assertFalse(self.playback_service.is_playing)
        self.assertGreater(len(self.mock_sink.written_chunks), chunks_before)
        # Ensure fresh audio was written
        self.assertEqual(self.mock_sink.written_chunks[-1][:2], b"\x02\x00")

    def test_blocking_playback_interrupted_without_deadlock(self):
        """Verify that calling stop() from another thread unblocks blocking play() without deadlocking."""
        long_audio = b"\x03\x00" * 16000  # 16000 samples = ~1 sec at 10ms pacing
        started_event = threading.Event()
        finished_event = threading.Event()

        def run_blocking_play():
            started_event.set()
            self.playback_service.play(long_audio, non_blocking=False)
            finished_event.set()

        t = threading.Thread(target=run_blocking_play, daemon=True)
        t.start()
        self.assertTrue(started_event.wait(timeout=1.0))
        time.sleep(0.03)

        # Stop from another thread
        self.playback_service.stop()
        # Should finish quickly without deadlock
        finished = finished_event.wait(timeout=1.0)
        self.assertTrue(finished, "play(non_blocking=False) deadlocked or failed to return after stop().")
        self.assertFalse(self.playback_service.is_playing)

    def test_overlapping_playback_prevention_on_unresponsive_worker(self):
        """Verify that if a worker fails to terminate on abort, starting another raises AudioDeviceError."""
        unresponsive_sink = MockAudioOutputSink()
        hang_event = threading.Event()

        def hanging_write(data):
            hang_event.wait(timeout=2.0)

        unresponsive_sink.write = hanging_write
        service = AudioPlaybackService(sink=unresponsive_sink, config=self.config)

        audio = b"\x01\x00" * 320
        service.play(audio, non_blocking=True)
        time.sleep(0.02)
        self.assertTrue(service.is_playing)

        # Attempting to play again should attempt abort; since worker hangs, raise AudioDeviceError
        with self.assertRaises(AudioDeviceError):
            service.play(audio, non_blocking=False)

        # Clean up hanging worker
        hang_event.set()
        service.shutdown()


class TestCaptureShutdownAndWorkerSafety(unittest.TestCase):
    """Tests for capture worker shutdown unblocking and worker protection."""

    def test_capture_shutdown_unblocks_blocking_read(self):
        """Verify that capture service stop() closes the source first to unblock blocking read."""
        unblock_event = threading.Event()
        close_called_event = threading.Event()

        class BlockingInputSource(AudioInputSource):
            def __init__(self):
                self._active = False

            def open(self):
                self._active = True

            def read(self, num_samples):
                if not self._active:
                    raise AudioReadError("Inactive")
                # Block until close is called
                close_called_event.wait(timeout=2.0)
                if not self._active:
                    raise AudioReadError("Closed")
                return b"\x00\x00" * num_samples

            def close(self):
                self._active = False
                close_called_event.set()

            @property
            def is_active(self):
                return self._active

        src = BlockingInputSource()
        cfg = VoiceConfig(frame_duration_ms=10)
        service = AudioCaptureService(source=src, config=cfg)
        service.start()
        self.assertTrue(service.is_running)

        start_time = time.time()
        service.stop()
        elapsed = time.time() - start_time
        self.assertFalse(service.is_running)
        self.assertLess(elapsed, 1.0, "Capture stop() took too long to unblock.")

    def test_capture_start_rejected_if_worker_alive(self):
        """Verify that start() raises AudioDeviceError if previous capture worker is still alive."""
        hang_event = threading.Event()

        class UnstoppableSource(AudioInputSource):
            def __init__(self):
                self._active = False

            def open(self):
                self._active = True

            def read(self, num_samples):
                hang_event.wait(timeout=2.0)
                return b"\x00\x00" * num_samples

            def close(self):
                # Deliberately do not unblock read
                pass

            @property
            def is_active(self):
                return self._active

        src = UnstoppableSource()
        cfg = VoiceConfig(frame_duration_ms=10)
        service = AudioCaptureService(source=src, config=cfg)
        service.start()

        # Artificially clear running flag while leaving worker alive to simulate stubborn worker
        service._running = False
        with self.assertRaises(AudioDeviceError):
            service.start()

        # Cleanup
        hang_event.set()
        src._active = False


class TestSoundDeviceBackends(unittest.TestCase):
    """Tests for SoundDeviceInputSource and SoundDeviceOutputSink error handling."""

    @patch("sounddevice.query_devices", return_value=[])
    def test_sounddevice_input_no_devices(self, mock_query):
        src = SoundDeviceInputSource()
        with self.assertRaises(AudioDeviceError):
            src.open()

    @patch("sounddevice.query_devices", return_value=[])
    def test_sounddevice_output_no_devices(self, mock_query):
        sink = SoundDeviceOutputSink()
        with self.assertRaises(AudioDeviceError):
            sink.open()


if __name__ == "__main__":
    unittest.main()
