"""
JARVIS Voice Subsystem - Milestone 5.1.1
Local Audio I/O and Voice Activity Detection Foundation.
"""

from voice.config import VoiceConfig, default_voice_config
from voice.audio_io import (
    AudioInputSource,
    AudioOutputSink,
    AudioCaptureService,
    AudioPlaybackService,
    AudioDeviceError,
    AudioConfigError,
    AudioReadError,
    AudioWriteError,
)
from voice.vad import (
    VoiceActivityDetector,
    VADEvent,
    VADEventType,
    InvalidFrameError,
)

__all__ = [
    "VoiceConfig",
    "default_voice_config",
    "AudioInputSource",
    "AudioOutputSink",
    "AudioCaptureService",
    "AudioPlaybackService",
    "AudioDeviceError",
    "AudioConfigError",
    "AudioReadError",
    "AudioWriteError",
    "VoiceActivityDetector",
    "VADEvent",
    "VADEventType",
    "InvalidFrameError",
]
