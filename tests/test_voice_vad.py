"""
Unit and integration tests for JARVIS Voice Activity Detection (VAD) (Milestone 5.1.1).
Tests speech activity classification, silence accumulation, speech-start/speech-end transitions,
utterance buffer extraction, and invalid frame rejection.
"""

import math
import struct
import unittest

from voice.config import VoiceConfig
from voice.vad import (
    VoiceActivityDetector,
    VADEvent,
    InvalidFrameError,
)


def generate_pcm_frame(
    duration_ms: int = 20,
    sample_rate: int = 16000,
    frequency: float = 300.0,
    amplitude: float = 20000.0,
) -> bytes:
    """Generate synthetic 16-bit PCM sinusoidal speech/tone frame."""
    num_samples = (sample_rate * duration_ms) // 1000
    samples = []
    for i in range(num_samples):
        t = float(i) / float(sample_rate)
        val = int(amplitude * math.sin(2.0 * math.pi * frequency * t))
        val = max(-32768, min(32767, val))
        samples.append(val)
    return struct.pack(f"<{num_samples}h", *samples)


def generate_silence_frame(duration_ms: int = 20, sample_rate: int = 16000) -> bytes:
    """Generate synthetic 16-bit PCM pure silence frame."""
    num_samples = (sample_rate * duration_ms) // 1000
    return b"\x00\x00" * num_samples


class TestVoiceActivityDetector(unittest.TestCase):
    """Tests for VoiceActivityDetector frame processing and event generation."""

    def setUp(self):
        # Frame duration: 20ms, silence threshold: 200ms (10 frames of silence), min speech: 40ms (2 frames)
        self.config = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            vad_silence_duration_ms=200,
            vad_min_speech_duration_ms=40,
        )
        self.vad = VoiceActivityDetector(self.config)
        self.speech_frame = generate_pcm_frame(20, 16000, frequency=400.0, amplitude=25000.0)
        self.silence_frame = generate_silence_frame(20, 16000)

    def test_single_frame_speech_detection(self):
        is_speech = self.vad.is_speech(self.speech_frame)
        self.assertTrue(is_speech)

    def test_single_frame_silence_detection(self):
        is_speech = self.vad.is_speech(self.silence_frame)
        self.assertFalse(is_speech)

    def test_speech_start_event_generation(self):
        # First speech frame (20ms) - less than min_speech_duration (40ms)
        events1 = self.vad.process_frame(self.speech_frame)
        self.assertFalse(self.vad.is_speech_active)
        self.assertEqual(len(events1), 1)
        self.assertEqual(events1[0].event_type, "speech_frame")

        # Second speech frame (40ms) - reaches min_speech_duration!
        events2 = self.vad.process_frame(self.speech_frame)
        self.assertTrue(self.vad.is_speech_active)
        event_types = [e.event_type for e in events2]
        self.assertIn("speech_start", event_types)
        self.assertIn("speech_frame", event_types)

    def test_end_of_speech_after_configured_silence(self):
        # Trigger speech start (2 frames = 40ms)
        self.vad.process_frame(self.speech_frame)
        self.vad.process_frame(self.speech_frame)
        self.assertTrue(self.vad.is_speech_active)

        # Feed 9 frames of silence (180ms) -> should NOT trigger speech_end yet
        for _ in range(9):
            evs = self.vad.process_frame(self.silence_frame)
            self.assertTrue(self.vad.is_speech_active)
            self.assertEqual(evs[0].event_type, "silence_frame")

        # 10th frame of silence (200ms reached) -> triggers speech_end!
        final_events = self.vad.process_frame(self.silence_frame)
        self.assertFalse(self.vad.is_speech_active)
        event_types = [e.event_type for e in final_events]
        self.assertIn("speech_end", event_types)

        end_event = [e for e in final_events if e.event_type == "speech_end"][0]
        self.assertIsNotNone(end_event.accumulated_audio)
        # Total frames in utterance: 2 speech frames + 10 silence frames = 12 frames
        expected_bytes = 12 * self.config.frame_bytes
        self.assertEqual(len(end_event.accumulated_audio), expected_bytes)

    def test_invalid_frame_length_rejected(self):
        invalid_short_frame = b"\x00" * 100  # Expected 640 bytes
        with self.assertRaises(InvalidFrameError):
            self.vad.process_frame(invalid_short_frame)

    def test_empty_frame_rejected(self):
        with self.assertRaises(InvalidFrameError):
            self.vad.process_frame(b"")

    def test_invalid_type_rejected(self):
        with self.assertRaises(InvalidFrameError):
            self.vad.process_frame("not_bytes")

    def test_repeated_speech_segments(self):
        # Segment 1
        self.vad.process_frame(self.speech_frame)
        self.vad.process_frame(self.speech_frame)
        self.assertTrue(self.vad.is_speech_active)

        # Silence for Segment 1 to end (10 frames = 200ms)
        for _ in range(10):
            evs = self.vad.process_frame(self.silence_frame)
        self.assertFalse(self.vad.is_speech_active)
        self.assertEqual(evs[-1].event_type, "speech_end")

        # Segment 2
        evs_seg2_start = self.vad.process_frame(self.speech_frame)
        self.vad.process_frame(self.speech_frame)
        self.assertTrue(self.vad.is_speech_active)

        # Silence for Segment 2 to end
        for _ in range(10):
            evs2 = self.vad.process_frame(self.silence_frame)
        self.assertFalse(self.vad.is_speech_active)
        self.assertEqual(evs2[-1].event_type, "speech_end")

    def test_bounded_utterance_buffer(self):
        # Configure small buffer duration: 0.1s (5 frames max)
        small_buf_cfg = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            max_buffer_duration_sec=0.1,  # 1600 samples = 3200 bytes = 5 frames
            vad_silence_duration_ms=1000,
            vad_min_speech_duration_ms=20,
        )
        vad = VoiceActivityDetector(small_buf_cfg)

        # Feed 15 speech frames (300ms)
        for _ in range(15):
            vad.process_frame(self.speech_frame)

        # Buffer should be bounded and not exceed max_buffer_bytes
        self.assertLessEqual(len(vad._speech_buffer), small_buf_cfg.max_buffer_bytes)
        self.assertTrue(vad.is_truncated)

    def test_pre_speech_buffer_bounding(self):
        # max_pre_speech_duration_ms = 40 (2 frames = 1280 bytes)
        # vad_min_speech_duration_ms = 80 (4 frames needed to confirm speech)
        cfg = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            max_pre_speech_duration_ms=40,
            vad_min_speech_duration_ms=80,
        )
        vad = VoiceActivityDetector(cfg)

        # Feed 3 speech frames (60ms < 80ms)
        for _ in range(3):
            vad.process_frame(self.speech_frame)

        # Speech should not be active yet
        self.assertFalse(vad.is_speech_active)
        # Pre-speech buffer must be bounded to max_pre_speech_bytes (1280 bytes)
        self.assertEqual(vad.pre_speech_buffered_bytes, cfg.max_pre_speech_bytes)

    def test_buffer_overflow_event_and_speech_end_reporting(self):
        cfg = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            max_buffer_duration_sec=0.08,  # 4 frames max = 2560 bytes
            vad_min_speech_duration_ms=20,
            vad_silence_duration_ms=60,  # 3 frames of silence to end
        )
        vad = VoiceActivityDetector(cfg)

        overflow_events = []
        # Feed 6 speech frames (exceeds 4 frames limit)
        for _ in range(6):
            events = vad.process_frame(self.speech_frame)
            for ev in events:
                if ev.event_type == "buffer_overflow":
                    overflow_events.append(ev)

        self.assertEqual(len(overflow_events), 1)
        self.assertTrue(overflow_events[0].truncated)
        self.assertTrue(vad.is_truncated)

        # Feed 3 silence frames to trigger speech_end
        end_events = []
        for _ in range(3):
            events = vad.process_frame(self.silence_frame)
            end_events.extend([ev for ev in events if ev.event_type == "speech_end"])

        self.assertEqual(len(end_events), 1)
        # speech_end must report that utterance was truncated
        self.assertTrue(end_events[0].truncated)
        # After speech_end, state resets
        self.assertFalse(vad.is_truncated)
        self.assertFalse(vad.is_speech_active)

    def test_configurable_rms_energy_thresholds(self):
        custom_cfg = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            silence_rms_threshold=200.0,
            speech_rms_threshold=600.0,
        )
        vad = VoiceActivityDetector(custom_cfg)
        self.assertEqual(vad.config.silence_rms_threshold, 200.0)
        self.assertEqual(vad.config.speech_rms_threshold, 600.0)

    def test_reset(self):
        cfg = VoiceConfig(
            sample_rate=16000,
            frame_duration_ms=20,
            max_buffer_duration_sec=0.04,  # 2 frames max
            vad_min_speech_duration_ms=20,
        )
        vad = VoiceActivityDetector(cfg)
        # Feed 4 frames to trigger truncation
        for _ in range(4):
            vad.process_frame(self.speech_frame)
        self.assertTrue(vad.is_speech_active)
        self.assertTrue(vad.is_truncated)

        vad.reset()
        self.assertFalse(vad.is_speech_active)
        self.assertFalse(vad.is_truncated)
        self.assertEqual(len(vad._speech_buffer), 0)
        self.assertEqual(vad.buffered_duration_sec, 0.0)
        self.assertEqual(vad.pre_speech_buffered_bytes, 0)


if __name__ == "__main__":
    unittest.main()
