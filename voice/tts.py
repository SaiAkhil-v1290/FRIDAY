"""
Local Text-to-Speech (TTS) Subsystem for JARVIS using Piper TTS (Milestone 5.1.6).
Provides local-only, streaming neural speech synthesis with bounded in-memory audio buffering,
thread-safe playback session serialization, immediate cancellation, and mockable interfaces.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import threading
from typing import Any, Callable, Iterator, Optional

from voice.audio_io import AudioDeviceError, AudioOutputSink, SoundDeviceOutputSink
from voice.config import VoiceConfig, default_voice_config

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Custom TTS Exceptions
# =============================================================================

class TTSError(Exception):
    """Base exception for all text-to-speech subsystem failures."""
    pass


class TTSInitializationError(TTSError):
    """Raised when the Piper engine or voice model fails to load or configure."""
    pass


class TTSUnavailableError(TTSError):
    """Raised when text synthesis is requested but the TTS service is unavailable."""
    pass


class TTSConfigError(TTSError):
    """Raised when text input or synthesis configuration violates safety bounds."""
    pass


class TTSPlaybackError(TTSError):
    """Raised when audio playback fails or an uncooperative worker cannot be terminated."""
    pass


# =============================================================================
# 2. Session Representation
# =============================================================================

@dataclass
class _TTSSession:
    """Internal tracker for an active or completing TTS synthesis and playback session."""
    session_id: int
    text: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    worker_thread: Optional[threading.Thread] = None
    error: Optional[Exception] = None
    cancelled: bool = False


# =============================================================================
# 3. Piper TTS Service
# =============================================================================

class PiperTTSService:
    """
    Local-only, streaming Text-to-Speech service using Piper TTS.
    Converts supplied text into linear PCM speech audio and streams it to the speaker sink.

    Guarantees:
    - Lazy loading: importing or constructing does not fail if Piper or model is missing.
    - Model caching: loaded voice models are retained in memory across utterances.
    - Model and config validation: checks existence and integrity of local .onnx and .onnx.json files.
    - Zero cloud dependency: never contacts external networks or auto-downloads models.
    - In-memory processing: audio is streamed chunk-by-chunk in memory without disk persistence.
    - Sequential and serialized playback: strictly one active speech session at any given moment.
    - Non-deadlocking cancellation: stop() safely interrupts both streaming synthesis and playback.
    - Preserves native Piper audio format: adapts dynamically to model sample rate (e.g. 22050 Hz).
    """

    def __init__(
        self,
        config: Optional[VoiceConfig] = None,
        sink: Optional[AudioOutputSink] = None,
        voice: Optional[Any] = None,
        voice_loader: Optional[Callable[[str, Optional[str]], Any]] = None,
    ) -> None:
        self.config = config or default_voice_config
        self.sink = sink or SoundDeviceOutputSink(self.config)

        # Injected voice model or custom loader (primarily for deterministic unit testing)
        self._voice_override = voice
        self._voice_loader = voice_loader

        # Model caching and synchronization
        self._model_lock = threading.Lock()
        self._model: Optional[Any] = voice
        self._initialization_error: Optional[str] = None
        self._last_error: Optional[Exception] = None

        # Playback session state and synchronization
        self._lifecycle_lock = threading.Lock()
        self._current_session: Optional[_TTSSession] = None
        self._session_counter: int = 0
        self._is_speaking: bool = False

    @property
    def is_speaking(self) -> bool:
        """Return True if speech synthesis or playback is actively occurring."""
        with self._lifecycle_lock:
            return self._current_session is not None and not self._current_session.done_event.is_set()

    @property
    def last_error(self) -> Optional[Exception]:
        """Return the most recent exception encountered during synthesis or playback."""
        return self._last_error

    @property
    def initialization_error(self) -> Optional[str]:
        """Return error message describing why Piper or the voice model is unavailable, if any."""
        return self._initialization_error

    def is_available(self) -> bool:
        """
        Check whether Piper and the configured voice model are ready for speech synthesis.
        Lazily attempts to validate configuration and load the model without raising.
        """
        try:
            model = self._ensure_model_loaded()
            return model is not None
        except Exception as exc:
            self._initialization_error = str(exc)
            return False

    def speak(self, text: str, non_blocking: bool = True) -> None:
        """
        Convert text into speech and play it through the audio output sink.

        Args:
            text: Non-empty string to synthesize.
            non_blocking: If True (default), synthesis and playback run on a dedicated
                daemon thread. If False, the call blocks until playback completes or is cancelled.

        Raises:
            TTSConfigError: If text is empty, whitespace-only, or exceeds max length.
            TTSUnavailableError: If Piper or the configured voice model is unavailable.
            TTSPlaybackError: If audio playback fails or a prior session cannot be cancelled.
        """
        # 1. Text input validation
        if text is None or not isinstance(text, str):
            raise TTSConfigError("Text must be a non-empty string.")

        clean_text = text.strip()
        if not clean_text:
            raise TTSConfigError("Cannot synthesize empty or whitespace-only text.")

        if len(text) > self.config.tts_max_text_length:
            raise TTSConfigError(
                f"Input text length ({len(text)}) exceeds maximum allowed "
                f"limit of {self.config.tts_max_text_length} characters."
            )

        # 2. Ensure model is available
        model = self._ensure_model_loaded()
        if model is None:
            err_msg = self._initialization_error or "Piper TTS service or voice model is unavailable."
            raise TTSUnavailableError(err_msg)

        # 3. Coordinate session serialization under lifecycle lock
        worker_to_join: Optional[threading.Thread] = None
        prior_session_to_abort: Optional[_TTSSession] = None

        with self._lifecycle_lock:
            # Check if there is an active session
            if self._current_session is not None and not self._current_session.done_event.is_set():
                prior_session_to_abort = self._current_session
                prior_session_to_abort.stop_event.set()
                prior_session_to_abort.cancelled = True
                try:
                    self.sink.stop()
                except Exception as e:
                    logger.debug("Error stopping sink to preempt prior session: %s", e)
                worker_to_join = prior_session_to_abort.worker_thread

        # Join prior worker OUTSIDE lifecycle lock to avoid deadlock
        if worker_to_join is not None and worker_to_join is not threading.current_thread():
            if worker_to_join.is_alive():
                worker_to_join.join(timeout=self.config.tts_playback_join_timeout_seconds)
                if worker_to_join.is_alive():
                    # Prior session refused to stop!
                    raise TTSPlaybackError(
                        "Cannot start speech playback: previous session failed to terminate within timeout."
                    )

        # 4. Start new session under lifecycle lock
        with self._lifecycle_lock:
            self._session_counter += 1
            session = _TTSSession(
                session_id=self._session_counter,
                text=clean_text,
            )
            self._current_session = session
            self._is_speaking = True
            self._last_error = None

            if non_blocking:
                worker = threading.Thread(
                    target=self._run_session,
                    args=(session,),
                    name=f"PiperTTSWorker-{session.session_id}",
                    daemon=True,
                )
                session.worker_thread = worker
                worker.start()
                return

        # Blocking execution runs on calling thread
        session.worker_thread = threading.current_thread()
        self._run_session(session)
        if session.error is not None:
            raise session.error

    def stop(self) -> None:
        """
        Immediately interrupt and cancel any active speech synthesis and playback.
        Thread-safe and idempotent.
        """
        worker_to_join: Optional[threading.Thread] = None
        session: Optional[_TTSSession] = None

        with self._lifecycle_lock:
            if self._current_session is None or self._current_session.done_event.is_set():
                self._is_speaking = False
                return

            session = self._current_session
            session.stop_event.set()
            session.cancelled = True

            try:
                self.sink.stop()
            except Exception as e:
                logger.debug("Error stopping audio sink during TTS stop: %s", e)

            worker_to_join = session.worker_thread

        # Wait for worker outside lock
        if worker_to_join is not None and worker_to_join is not threading.current_thread():
            if worker_to_join.is_alive():
                worker_to_join.join(timeout=self.config.tts_playback_join_timeout_seconds)
                if worker_to_join.is_alive():
                    raise TTSPlaybackError(
                        f"TTS worker thread {worker_to_join.name} failed to terminate within "
                        f"{self.config.tts_playback_join_timeout_seconds}s timeout."
                    )

        with self._lifecycle_lock:
            if self._current_session is session:
                self._current_session = None
                self._is_speaking = False

    def _ensure_model_loaded(self) -> Optional[Any]:
        """
        Lazily validate configuration and load the Piper voice model into memory.
        Thread-safe and cached.
        """
        with self._model_lock:
            if self._model is not None:
                return self._model

            # Check if voice model override is present
            if self._voice_override is not None:
                self._model = self._voice_override
                return self._model

            model_path = self.config.tts_model_path
            if not model_path:
                self._initialization_error = "TTS voice model path is not configured."
                return None

            # Validate local ONNX model file
            if not os.path.isfile(model_path):
                self._initialization_error = f"TTS voice model file not found: '{model_path}'"
                return None

            # Resolve and validate local JSON configuration
            config_path = self.config.tts_config_path or f"{model_path}.json"
            if not os.path.isfile(config_path):
                self._initialization_error = f"TTS voice model configuration file not found: '{config_path}'"
                return None

            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                if not isinstance(config_data, dict):
                    self._initialization_error = "Invalid TTS voice model configuration: root must be JSON object."
                    return None
            except Exception as e:
                self._initialization_error = f"Failed to parse TTS voice model JSON configuration: {e}"
                return None

            # Load Piper voice model via custom loader or piper-tts package
            try:
                if self._voice_loader is not None:
                    self._model = self._voice_loader(model_path, config_path)
                else:
                    try:
                        from piper import PiperVoice
                    except ImportError:
                        self._initialization_error = (
                            "Piper package ('piper-tts') is not installed. "
                            "Please install piper-tts to enable local speech synthesis."
                        )
                        return None

                    self._model = PiperVoice.load(model_path, config_path=config_path)

                self._initialization_error = None
                logger.info("Piper voice model loaded successfully from '%s'", model_path)
                return self._model

            except Exception as e:
                self._initialization_error = f"Failed to load Piper voice model: {e}"
                logger.error("Failed to load Piper voice model from '%s': %s", model_path, e)
                return None

    def _build_synthesis_config(self) -> Optional[Any]:
        """Build Piper SynthesisConfig parameters from VoiceConfig if piper is installed."""
        try:
            from piper import SynthesisConfig
            return SynthesisConfig(
                volume=self.config.tts_volume,
                length_scale=self.config.tts_length_scale,
                noise_scale=self.config.tts_noise_scale,
                noise_w_scale=self.config.tts_noise_w_scale,
            )
        except Exception:
            return None

    def _synthesize_stream(self, voice: Any, text: str, syn_config: Optional[Any] = None) -> Iterator[Any]:
        """
        Invoke Piper's streaming synthesis generator.
        Falls back to in-memory synthesize_wav if synthesize is not implemented on mock.
        """
        if hasattr(voice, "synthesize"):
            try:
                if syn_config is not None:
                    return voice.synthesize(text, syn_config=syn_config)
                return voice.synthesize(text)
            except TypeError:
                return voice.synthesize(text)
        elif hasattr(voice, "synthesize_wav"):
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav_out:
                if syn_config is not None:
                    try:
                        voice.synthesize_wav(text, wav_out, syn_config=syn_config)
                    except TypeError:
                        voice.synthesize_wav(text, wav_out)
                else:
                    voice.synthesize_wav(text, wav_out)
            buf.seek(0)
            with wave.open(buf, "rb") as wav_in:
                rate = wav_in.getframerate()
                width = wav_in.getsampwidth()
                channels = wav_in.getnchannels()
                pcm = wav_in.readframes(wav_in.getnframes())

            @dataclass
            class _SyntheticChunk:
                audio_int16_bytes: bytes
                sample_rate: int
                sample_width: int
                sample_channels: int

            return iter([_SyntheticChunk(pcm, rate, width, channels)])
        else:
            raise TTSError("Voice model does not support synthesize or synthesize_wav.")

    def _run_session(self, session: _TTSSession) -> None:
        """Sequential synthesis and playback worker routine."""
        sink_opened = False
        try:
            voice = self._model
            syn_config = self._build_synthesis_config()

            total_bytes = 0
            bytes_per_second: Optional[int] = None
            max_bytes: Optional[int] = None

            for chunk in self._synthesize_stream(voice, session.text, syn_config):
                # Cooperative cancellation check
                if session.stop_event.is_set() or self._current_session is not session:
                    session.cancelled = True
                    break

                audio_data = getattr(chunk, "audio_int16_bytes", None) or getattr(chunk, "audio_bytes", None)
                if audio_data is None:
                    if isinstance(chunk, bytes):
                        audio_data = chunk
                    else:
                        audio_data = bytes(chunk)

                if not audio_data:
                    continue

                sample_rate = getattr(chunk, "sample_rate", None) or getattr(voice, "sample_rate", 22050)
                sample_width = getattr(chunk, "sample_width", 2)
                channels = getattr(chunk, "sample_channels", 1)

                if bytes_per_second is None:
                    bytes_per_second = sample_rate * sample_width * channels
                    max_bytes = int(self.config.tts_max_audio_duration_seconds * bytes_per_second)

                total_bytes += len(audio_data)
                if max_bytes is not None and total_bytes > max_bytes:
                    raise TTSConfigError(
                        f"Generated audio exceeded maximum limit of "
                        f"{self.config.tts_max_audio_duration_seconds}s ({total_bytes} bytes)."
                    )

                # Open sink with actual audio format reported by model
                if not sink_opened or not self.sink.is_active:
                    try:
                        self.sink.open(sample_rate=sample_rate, channels=channels)
                    except TypeError:
                        self.sink.open()
                    sink_opened = True

                # Write audio chunk to output sink
                self.sink.write(audio_data)

        except Exception as exc:
            if not session.stop_event.is_set():
                session.error = exc
                logger.error("TTS playback session %d encountered an error: %s", session.session_id, exc)
        finally:
            if session.stop_event.is_set():
                session.cancelled = True

            session.done_event.set()
            with self._lifecycle_lock:
                if session.error is not None:
                    self._last_error = session.error
                if self._current_session is session:
                    self._current_session = None
                    self._is_speaking = False

