"""
JARVIS Voice Subsystem - Milestone 5.1
Local Audio I/O, Voice Activity Detection, and Speech-to-Text Foundation.
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
from voice.stt import (
    SpeechToTextService,
    TranscriptionResult,
    SpeechToTextError,
)
from voice.pipeline import (
    VoicePipeline,
    VoicePipelineEvent,
    VoicePipelineEventType,
    VoicePipelineError,
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
    "SpeechToTextService",
    "TranscriptionResult",
    "SpeechToTextError",
    "VoicePipeline",
    "VoicePipelineEvent",
    "VoicePipelineEventType",
    "VoicePipelineError",
]
