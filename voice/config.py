"""
Configuration settings for JARVIS Voice Subsystem (Milestone 5.1.1).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceConfig:
    """
    Configuration parameters for audio capture, playback, voice activity detection,
    and speech-to-text (STT) transcription.
    Enforces strict mathematical bounds to guarantee frame alignment and memory safety.
    """
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # 16-bit PCM = 2 bytes per sample
    frame_duration_ms: int = 20  # 10, 20, or 30 ms (required by webrtcvad)
    vad_mode: int = 2  # 0: normal, 1: low bitrate, 2: aggressive, 3: very aggressive
    vad_silence_duration_ms: int = 800  # End-of-speech silence threshold
    vad_min_speech_duration_ms: int = 100  # Minimum continuous speech to trigger speech_start
    max_pre_speech_duration_ms: int = 500  # Bounded pre-speech onset buffer duration
    silence_rms_threshold: float = 150.0  # RMS floor below which audio is pure silence
    speech_rms_threshold: float = 450.0  # Energy fallback threshold for speech when WebRTC VAD unavailable
    max_buffer_duration_sec: float = 30.0  # Volatile audio retention bound
    max_queue_size: int = 1000  # Bounded queue capacity to prevent memory bloat
    whisper_executable_path: str | None = None
    whisper_model_path: str | None = None
    whisper_language: str = "auto"
    stt_timeout_seconds: float = 30.0
    max_audio_duration_seconds: float = 60.0
    stt_threads: int = 4
    stt_output_format: str = "txt"

    def __post_init__(self) -> None:
        if self.sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(
                f"Unsupported sample_rate={self.sample_rate}. Must be 8000, 16000, 32000, or 48000 Hz."
            )
        if self.channels != 1:
            raise ValueError(f"Only mono audio (channels=1) is supported, got channels={self.channels}.")
        if self.sample_width != 2:
            raise ValueError(
                f"Only 16-bit linear PCM (sample_width=2) is supported, got sample_width={self.sample_width}."
            )
        if self.frame_duration_ms not in (10, 20, 30):
            raise ValueError(
                f"Unsupported frame_duration_ms={self.frame_duration_ms}. webrtcvad requires 10, 20, or 30 ms."
            )
        if self.vad_mode not in (0, 1, 2, 3):
            raise ValueError(f"vad_mode must be 0, 1, 2, or 3, got vad_mode={self.vad_mode}.")
        if self.vad_silence_duration_ms <= 0:
            raise ValueError("vad_silence_duration_ms must be positive.")
        if self.vad_min_speech_duration_ms < 0:
            raise ValueError("vad_min_speech_duration_ms cannot be negative.")
        if self.max_pre_speech_duration_ms <= 0:
            raise ValueError("max_pre_speech_duration_ms must be positive.")
        if self.silence_rms_threshold < 0:
            raise ValueError("silence_rms_threshold cannot be negative.")
        if self.speech_rms_threshold <= self.silence_rms_threshold:
            raise ValueError("speech_rms_threshold must be strictly greater than silence_rms_threshold.")
        if self.max_buffer_duration_sec <= 0:
            raise ValueError("max_buffer_duration_sec must be positive.")
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive.")
        if not self.whisper_language or not self.whisper_language.strip():
            raise ValueError("whisper_language cannot be empty.")
        if self.stt_timeout_seconds <= 0:
            raise ValueError("stt_timeout_seconds must be positive.")
        if self.max_audio_duration_seconds <= 0:
            raise ValueError("max_audio_duration_seconds must be positive.")
        if self.stt_threads <= 0:
            raise ValueError("stt_threads must be positive.")
        if self.stt_output_format not in ("txt", "json", "stdout"):
            raise ValueError(f"Unsupported stt_output_format='{self.stt_output_format}'. Must be 'txt', 'json', or 'stdout'.")

    @property
    def bytes_per_sample(self) -> int:
        return self.channels * self.sample_width

    @property
    def frame_samples(self) -> int:
        return (self.sample_rate * self.frame_duration_ms) // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.bytes_per_sample

    @property
    def max_pre_speech_bytes(self) -> int:
        return (self.sample_rate * self.max_pre_speech_duration_ms // 1000) * self.bytes_per_sample

    @property
    def max_buffer_bytes(self) -> int:
        return int(self.sample_rate * self.max_buffer_duration_sec * self.bytes_per_sample)

    @property
    def max_audio_bytes(self) -> int:
        return int(self.sample_rate * self.max_audio_duration_seconds * self.bytes_per_sample)


default_voice_config = VoiceConfig()
