"""
Audio Input and Output Subsystem for JARVIS (Milestone 5.1.1).
Provides non-blocking, bounded audio capture and playback with strict lifecycle management
and complete mockability for automated testing.
"""

from abc import ABC, abstractmethod
import queue
import threading
import time
from typing import Callable

from voice.config import VoiceConfig, default_voice_config


# =============================================================================
# 1. Custom Audio Exception Hierarchy
# =============================================================================

class AudioError(Exception):
    """Base exception for all audio subsystem failures."""
    pass


class AudioDeviceError(AudioError):
    """Raised when an audio input or output device is missing, busy, or disconnected."""
    pass


class AudioConfigError(AudioError):
    """Raised when an invalid audio configuration or parameter is supplied."""
    pass


class AudioReadError(AudioError):
    """Raised when an error occurs reading raw PCM frames from an input source."""
    pass


class AudioWriteError(AudioError):
    """Raised when an error occurs writing raw PCM frames to an output sink."""
    pass


# =============================================================================
# 2. Audio Hardware Abstractions (Mockable Protocols)
# =============================================================================

class AudioInputSource(ABC):
    """Abstract base class for audio capture sources."""

    @abstractmethod
    def open(self) -> None:
        """Open and prepare the input capture stream."""
        pass

    @abstractmethod
    def read(self, num_samples: int) -> bytes:
        """
        Read the requested number of 16-bit linear PCM mono samples.
        Returns exact byte count (num_samples * sample_width).
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Close and release the input stream resources."""
        pass

    @property
    @abstractmethod
    def is_active(self) -> bool:
        """Return True if the input source is currently open and active."""
        pass


class AudioOutputSink(ABC):
    """Abstract base class for audio playback sinks."""

    @abstractmethod
    def open(self) -> None:
        """Open and prepare the output playback stream."""
        pass

    @abstractmethod
    def write(self, pcm_data: bytes) -> None:
        """Write raw 16-bit linear PCM mono bytes to the playback stream."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Immediately abort active playback and flush output buffers."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close and release the output stream resources."""
        pass

    @property
    @abstractmethod
    def is_active(self) -> bool:
        """Return True if the output sink is currently open and active."""
        pass


# =============================================================================
# 3. Default Hardware Implementations (SoundDevice Backend)
# =============================================================================

class SoundDeviceInputSource(AudioInputSource):
    """Default microphone capture backend using python-sounddevice."""

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or default_voice_config
        self._stream = None
        self._active = False

    def open(self) -> None:
        if self._active and self._stream is not None:
            return

        try:
            import sounddevice as sd
        except ImportError:
            raise AudioDeviceError(
                "SoundDevice backend is not installed. Please install sounddevice."
            )

        try:
            # Query default input device to verify hardware presence
            devices = sd.query_devices()
            if not devices:
                raise AudioDeviceError("No audio devices detected on this system.")

            default_in = sd.default.device[0]
            if default_in is None or default_in < 0:
                # Search for any valid input device
                found = False
                for idx, dev in enumerate(devices):
                    if dev.get("max_input_channels", 0) > 0:
                        default_in = idx
                        found = True
                        break
                if not found:
                    raise AudioDeviceError("No microphone or audio input device found.")

            self._stream = sd.RawInputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                blocksize=self.config.frame_samples,
                device=default_in,
            )
            self._stream.start()
            self._active = True
        except Exception as exc:
            self._active = False
            self._stream = None
            if isinstance(exc, AudioDeviceError):
                raise
            raise AudioDeviceError(f"Failed to open microphone input stream: {exc}") from exc

    def read(self, num_samples: int) -> bytes:
        if not self._active or self._stream is None:
            raise AudioReadError("Cannot read from closed or inactive audio input source.")

        try:
            expected_bytes = num_samples * self.config.bytes_per_sample
            data, overflowed = self._stream.read(num_samples)
            raw_bytes = bytes(data)
            if len(raw_bytes) != expected_bytes:
                raise AudioReadError(
                    f"Incomplete read: expected {expected_bytes} bytes, got {len(raw_bytes)}."
                )
            return raw_bytes
        except Exception as exc:
            self._active = False
            if isinstance(exc, AudioReadError):
                raise
            raise AudioDeviceError(f"Microphone disconnected or read error: {exc}") from exc

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None
                self._active = False

    @property
    def is_active(self) -> bool:
        return self._active and self._stream is not None


class SoundDeviceOutputSink(AudioOutputSink):
    """Default speaker playback backend using python-sounddevice."""

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or default_voice_config
        self._stream = None
        self._active = False

    def open(self) -> None:
        if self._active and self._stream is not None:
            return

        try:
            import sounddevice as sd
        except ImportError:
            raise AudioDeviceError(
                "SoundDevice backend is not installed. Please install sounddevice."
            )

        try:
            devices = sd.query_devices()
            if not devices:
                raise AudioDeviceError("No audio devices detected on this system.")

            default_out = sd.default.device[1]
            if default_out is None or default_out < 0:
                found = False
                for idx, dev in enumerate(devices):
                    if dev.get("max_output_channels", 0) > 0:
                        default_out = idx
                        found = True
                        break
                if not found:
                    raise AudioDeviceError("No speaker or audio output device found.")

            self._stream = sd.RawOutputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                device=default_out,
            )
            self._stream.start()
            self._active = True
        except Exception as exc:
            self._active = False
            self._stream = None
            if isinstance(exc, AudioDeviceError):
                raise
            raise AudioDeviceError(f"Failed to open speaker output stream: {exc}") from exc

    def write(self, pcm_data: bytes) -> None:
        if not self._active or self._stream is None:
            raise AudioWriteError("Cannot write to closed or inactive audio output sink.")

        try:
            self._stream.write(pcm_data)
        except Exception as exc:
            self._active = False
            raise AudioDeviceError(f"Audio output failure during playback: {exc}") from exc

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None
                self._active = False

    @property
    def is_active(self) -> bool:
        return self._active and self._stream is not None


# =============================================================================
# 4. Audio Capture Service (Bounded Buffering, Concurrency, Lifecycle)
# =============================================================================

class AudioCaptureService:
    """
    Manages non-blocking, volatile microphone capture.
    Maintains bounded memory queues, avoids disk writes, handles unexpected device
    disconnections gracefully, and provides an idempotent lifecycle.
    """

    def __init__(
        self,
        source: AudioInputSource | None = None,
        config: VoiceConfig | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.config = config or default_voice_config
        self.source = source or SoundDeviceInputSource(self.config)
        self.on_error = on_error

        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=self.config.max_queue_size)
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._running = False
        self._overflow_count = 0
        self._last_error: Exception | None = None

    @property
    def is_running(self) -> bool:
        return self._running and (self._worker_thread is not None and self._worker_thread.is_alive())

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def start(self) -> None:
        """
        Start the background capture worker.
        Idempotent: calling start() while already running does not spawn duplicate workers.
        """
        with self._lifecycle_lock:
            if self.is_running:
                return

            self._last_error = None
            self._overflow_count = 0
            self.clear_buffer()
            self._stop_event.clear()

            # Open hardware source
            self.source.open()

            self._running = True
            self._worker_thread = threading.Thread(
                target=self._capture_worker,
                name="JarvisAudioCaptureWorker",
                daemon=True,
            )
            self._worker_thread.start()

    def _capture_worker(self) -> None:
        """Continuous background capture loop feeding the bounded queue."""
        frame_samples = self.config.frame_samples

        while not self._stop_event.is_set():
            try:
                frame = self.source.read(frame_samples)
                if not frame:
                    continue

                # Enqueue into bounded queue
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    # Bounded queue protection: drop oldest frame to prevent unbounded memory growth
                    self._overflow_count += 1
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(frame)
                    except (queue.Empty, queue.Full):
                        pass

            except Exception as exc:
                self._last_error = exc
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
                break

        self._running = False

    def read_frame(self, timeout: float | None = 0.1) -> bytes | None:
        """
        Retrieve the next available PCM audio frame from volatile queue.
        Returns None if timeout expires without a frame.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear_buffer(self) -> None:
        """Flush and release all audio frames currently stored in the volatile queue."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """
        Cleanly and deterministically halt audio capture and release resources.
        Idempotent: safe to call multiple times.
        """
        with self._lifecycle_lock:
            if not self._running and self._worker_thread is None:
                return

            self._stop_event.set()
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=1.0)
            self._worker_thread = None

            self.source.close()
            self._running = False
            self.clear_buffer()

    def shutdown(self) -> None:
        """Deterministic alias for stop()."""
        self.stop()


# =============================================================================
# 5. Audio Playback Service (Immediate Cancellation, Non-Overlapping)
# =============================================================================

class AudioPlaybackService:
    """
    Manages non-blocking audio playback with strict interruption handling.
    Guarantees no overlapping playback sessions, supports instant cancellation,
    and handles missing output devices gracefully.
    """

    def __init__(
        self,
        sink: AudioOutputSink | None = None,
        config: VoiceConfig | None = None,
    ) -> None:
        self.config = config or default_voice_config
        self.sink = sink or SoundDeviceOutputSink(self.config)

        self._playback_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_playing = False
        self._last_error: Exception | None = None

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def play(self, pcm_data: bytes, non_blocking: bool = True) -> None:
        """
        Play raw 16-bit PCM audio.
        If a playback session is already active, it is immediately aborted before starting.
        """
        if not pcm_data:
            raise ValueError("Cannot play empty audio data.")

        if len(pcm_data) % self.config.bytes_per_sample != 0:
            raise AudioConfigError(
                f"Audio byte length ({len(pcm_data)}) is not aligned to sample width ({self.config.bytes_per_sample})."
            )

        with self._playback_lock:
            # Prevent overlapping playback sessions: abort any existing playback
            if self._is_playing:
                self._abort_internal()

            self._stop_event.clear()
            self._last_error = None
            self._is_playing = True

            if non_blocking:
                self._worker_thread = threading.Thread(
                    target=self._playback_worker,
                    args=(pcm_data,),
                    name="JarvisAudioPlaybackWorker",
                    daemon=True,
                )
                self._worker_thread.start()
            else:
                self._play_internal(pcm_data)

    def _playback_worker(self, pcm_data: bytes) -> None:
        self._play_internal(pcm_data)

    def _play_internal(self, pcm_data: bytes) -> None:
        """Chunked playback loop allowing immediate mid-stream cancellation."""
        chunk_bytes = self.config.frame_bytes
        total_len = len(pcm_data)
        offset = 0

        try:
            if not self.sink.is_active:
                self.sink.open()

            while offset < total_len and not self._stop_event.is_set():
                end = min(offset + chunk_bytes, total_len)
                chunk = pcm_data[offset:end]
                self.sink.write(chunk)
                offset = end

        except Exception as exc:
            self._last_error = exc
        finally:
            self._is_playing = False

    def _abort_internal(self) -> None:
        """Internal abort routine called under lock."""
        self._stop_event.set()
        self.sink.stop()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self._worker_thread = None
        self._is_playing = False

    def stop(self) -> None:
        """Immediately stop and abort active playback (Barge-in / Interruption)."""
        with self._playback_lock:
            self._abort_internal()

    def abort(self) -> None:
        """Alias for stop()."""
        self.stop()

    def shutdown(self) -> None:
        """Deterministic, idempotent shutdown closing output sink."""
        with self._playback_lock:
            self._abort_internal()
            self.sink.close()
