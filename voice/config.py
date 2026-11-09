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
    pipeline_event_queue_size: int = 200
    pipeline_frame_read_timeout_seconds: float = 0.1
    pipeline_join_timeout_seconds: float = 2.0
    pipeline_max_consecutive_read_errors: int = 5
    wake_word_phrase: str = "Hey Friday"
    wake_word_threshold: float = 0.5
    wake_word_cooldown_seconds: float = 2.0
    wake_word_model_path: str | None = None
    wake_word_model_key: str | None = None
    wake_word_engine: str = "openwakeword"
    wake_word_inference_framework: str = "onnx"
    wake_word_buffer_duration_sec: float = 3.0
    activation_event_queue_size: int = 200
    activation_frame_read_timeout_seconds: float = 0.1
    activation_pipeline_event_timeout_seconds: float = 0.1
    activation_join_timeout_seconds: float = 2.0
    tts_model_path: str | None = None
    tts_config_path: str | None = None
    tts_max_text_length: int = 1000
    tts_max_audio_duration_seconds: float = 60.0
    tts_playback_join_timeout_seconds: float = 2.0
    tts_volume: float = 1.0
    tts_length_scale: float = 1.0
    tts_noise_scale: float = 0.667
    tts_noise_w_scale: float = 0.8

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
        if self.pipeline_event_queue_size <= 0:
            raise ValueError("pipeline_event_queue_size must be positive.")
        if self.pipeline_frame_read_timeout_seconds <= 0:
            raise ValueError("pipeline_frame_read_timeout_seconds must be positive.")
        if self.pipeline_join_timeout_seconds <= 0:
            raise ValueError("pipeline_join_timeout_seconds must be positive.")
        if self.pipeline_max_consecutive_read_errors <= 0:
            raise ValueError("pipeline_max_consecutive_read_errors must be positive.")
        if not self.wake_word_phrase or not self.wake_word_phrase.strip():
            raise ValueError("wake_word_phrase cannot be empty.")
        if not (0.0 <= self.wake_word_threshold <= 1.0):
            raise ValueError("wake_word_threshold must be between 0.0 and 1.0.")
        if self.wake_word_cooldown_seconds < 0:
            raise ValueError("wake_word_cooldown_seconds cannot be negative.")
        if self.wake_word_buffer_duration_sec <= 0:
            raise ValueError("wake_word_buffer_duration_sec must be positive.")
        if self.wake_word_engine not in ("openwakeword", "custom"):
            raise ValueError(f"Unsupported wake_word_engine='{self.wake_word_engine}'. Must be 'openwakeword' or 'custom'.")
        if self.wake_word_inference_framework != "onnx":
            raise ValueError(f"Unsupported wake_word_inference_framework='{self.wake_word_inference_framework}'. Only 'onnx' is supported.")
        if self.activation_event_queue_size <= 0:
            raise ValueError("activation_event_queue_size must be positive.")
        if self.activation_frame_read_timeout_seconds <= 0:
            raise ValueError("activation_frame_read_timeout_seconds must be positive.")
        if self.activation_pipeline_event_timeout_seconds <= 0:
            raise ValueError("activation_pipeline_event_timeout_seconds must be positive.")
        if self.activation_join_timeout_seconds <= 0:
            raise ValueError("activation_join_timeout_seconds must be positive.")
        if self.tts_model_path is not None and not self.tts_model_path.strip():
            raise ValueError("tts_model_path cannot be empty.")
        if self.tts_config_path is not None and not self.tts_config_path.strip():
            raise ValueError("tts_config_path cannot be empty.")
        if self.tts_max_text_length <= 0:
            raise ValueError("tts_max_text_length must be positive.")
        if self.tts_max_audio_duration_seconds <= 0:
            raise ValueError("tts_max_audio_duration_seconds must be positive.")
        if self.tts_playback_join_timeout_seconds <= 0:
            raise ValueError("tts_playback_join_timeout_seconds must be positive.")
        if self.tts_volume <= 0.0:
            raise ValueError("tts_volume must be positive.")
        if self.tts_length_scale <= 0.0:
            raise ValueError("tts_length_scale must be positive.")
        if self.tts_noise_scale < 0.0:
            raise ValueError("tts_noise_scale cannot be negative.")
        if self.tts_noise_w_scale < 0.0:
            raise ValueError("tts_noise_w_scale cannot be negative.")

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
