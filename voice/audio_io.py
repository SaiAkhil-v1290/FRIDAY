"""
Audio Input and Output Subsystem for JARVIS (Milestone 5.1.1.2).
Provides non-blocking, bounded audio capture and playback with hardened lifecycle management,
non-deadlocking playback cancellation, safe capture shutdown, and complete mockability.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    def open(self, sample_rate: int | None = None, channels: int | None = None) -> None:
        """Open and prepare the output playback stream."""
        pass

    @abstractmethod
    def write(self, pcm_data: bytes) -> None:
        """Write raw 16-bit linear PCM mono bytes to the playback stream."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Immediately abort active playback, close stream, and release resources."""
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
            devices = sd.query_devices()
            if not devices:
                raise AudioDeviceError("No audio devices detected on this system.")

            default_in = sd.default.device[0]
            if default_in is None or default_in < 0:
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
        """
        Close the active input stream.
        Note: PortAudio C API is not reentrant across threads. Closing or aborting a stream
        while another thread is actively executing a blocking Pa_ReadStream call can lead
        to backend driver warnings or errors on non-compliant host APIs.
        Setting active to False beforehand ensures the reader thread aborts promptly.
        """
        self._active = False
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None

    @property
    def is_active(self) -> bool:
        return self._active and self._stream is not None


class SoundDeviceOutputSink(AudioOutputSink):
    """
    Default speaker playback backend using python-sounddevice.
    Hardened to ensure cancelled streams are completely closed and released,
    guaranteeing reliable reinitialization on subsequent playback cycles.
    Supports dynamic sample rates (e.g. For TTS output).
    """

    def __init__(
        self,
        config: VoiceConfig | None = None,
        sample_rate: int | None = None,
        channels: int | None = None,
    ) -> None:
        self.config = default_voice_config if config is None else config
        default_rate = self.config.sample_rate if self.config is not None else 16000
        default_channels = self.config.channels if self.config is not None else 1
        self.sample_rate = default_rate if sample_rate is None else sample_rate
        self.channels = default_channels if channels is None else channels
        self._stream = None
        self._active = False

    def open(self, sample_rate: int | None = None, channels: int | None = None) -> None:
        target_rate = self.sample_rate if sample_rate is None else sample_rate
        target_channels = self.channels if channels is None else channels

        if type(target_rate) is not int or target_rate <= 0:
            raise AudioDeviceError(f"Invalid output sample rate {target_rate}: must be an exact positive integer.")
        if type(target_channels) is not int or target_channels != 1:
            raise AudioDeviceError(f"Unsupported output channel count {target_channels}: only mono (1 channel as int) is supported.")

        if self._active and self._stream is not None:
            if self.sample_rate == target_rate and self.channels == target_channels:
                return
            self.close()

        self.sample_rate = target_rate
        self.channels = target_channels

        # Clean up any stale or aborted stream
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._active = False

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
                samplerate=self.sample_rate,
                channels=self.channels,
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

        if not isinstance(pcm_data, (bytes, bytearray, memoryview)):
            raise AudioWriteError("Audio data must be bytes-like.")

        if len(pcm_data) == 0:
            return

        frame_size = 2 * self.channels
        if len(pcm_data) % frame_size != 0:
            raise AudioWriteError(
                f"Audio buffer length {len(pcm_data)} bytes is not aligned to frame size {frame_size} bytes."
            )

        try:
            self._stream.write(pcm_data)
        except Exception as exc:
            self._active = False
            raise AudioDeviceError(f"Audio output failure during playback: {exc}") from exc

    def stop(self) -> None:
        """Immediately abort, close, and release the active stream, resetting internal active state."""
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None
                self._active = False

    def close(self) -> None:
        self.stop()

    @property
    def is_active(self) -> bool:
        return self._active and self._stream is not None


# =============================================================================
# 4. Audio Capture Service (Hardened Shutdown & Lifecycle Safety)
# =============================================================================

class AudioCaptureService:
    """
    Manages non-blocking, volatile microphone capture.
    Hardened shutdown sequence releases the input source before joining the worker
    so blocking reads unblock reliably, avoiding abandoned worker threads.
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
        self._is_stopping = False
        self._session_id = 0
        self._overflow_count = 0
        self._last_error: Exception | None = None

    @property
    def is_running(self) -> bool:
        return (
            self._running
            and not self._is_stopping
            and (self._worker_thread is not None and self._worker_thread.is_alive())
        )

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def start(self) -> None:
        """
        Start the background capture worker.
        Rejects starting if a previous capture worker is still active or finalizing shutdown.
        """
        with self._lifecycle_lock:
            if self.is_running:
                return

            if self._is_stopping:
                raise AudioDeviceError(
                    "Cannot start capture: previous capture session is still finalizing shutdown."
                )

            if self._worker_thread is not None and self._worker_thread.is_alive():
                raise AudioDeviceError(
                    "Cannot start capture: previous capture worker is still active."
                )

            self._last_error = None
            self._overflow_count = 0
            self.clear_buffer()
            self._stop_event.clear()

            # Open hardware source
            self.source.open()

            self._session_id += 1
            session_id = self._session_id
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._capture_worker,
                args=(session_id,),
                name=f"JarvisAudioCaptureWorker-{session_id}",
                daemon=True,
            )
            self._worker_thread.start()

    def _capture_worker(self, session_id: int) -> None:
        """Continuous background capture loop feeding the bounded queue."""
        frame_samples = self.config.frame_samples

        while not self._stop_event.is_set() and self._session_id == session_id:
            try:
                frame = self.source.read(frame_samples)
                if not frame:
                    continue

                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    # Bounded queue protection: drop oldest frame
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

        with self._lifecycle_lock:
            if self._session_id == session_id:
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
        Avoids holding lifecycle locks while waiting for the worker thread.
        Releases the input source before joining the worker thread to unblock reads.
        Prevents starting new capture sessions while shutdown is in progress.
        Guarantees stale shutdown finalization cannot corrupt newer sessions.
        Idempotent: safe to call multiple times.
        """
        worker_to_join: threading.Thread | None = None
        session_to_stop: int = 0

        with self._lifecycle_lock:
            if not self._running and (self._worker_thread is None or not self._worker_thread.is_alive()):
                return

            if self._is_stopping:
                worker_to_join = self._worker_thread
                session_to_stop = self._session_id
            else:
                self._is_stopping = True
                session_to_stop = self._session_id
                self._stop_event.set()

                # 1. Release or interrupt input source FIRST so blocked reads unblock
                try:
                    self.source.close()
                except Exception:
                    pass

                worker_to_join = self._worker_thread

        # 2. Wait for the worker to terminate WITHOUT holding _lifecycle_lock
        if worker_to_join is not None and worker_to_join.is_alive():
            worker_to_join.join(timeout=1.0)

        # 3. Finalize state under lock; preserve worker reference if it failed to terminate
        with self._lifecycle_lock:
            if self._session_id == session_to_stop:
                if not self._is_stopping and not self._running:
                    return

                if worker_to_join is not None and worker_to_join.is_alive():
                    self._running = False
                    self._is_stopping = False
                    raise AudioDeviceError(
                        "Audio capture worker failed to terminate during shutdown."
                    )

                self._worker_thread = None
                self._running = False
                self._is_stopping = False
                self.clear_buffer()

    def shutdown(self) -> None:
        """Deterministic alias for stop()."""
        self.stop()


# =============================================================================
# 5. Audio Playback Service (Immediate Cancellation, Session Ownership)
# =============================================================================

@dataclass
class _PlaybackSession:
    """Represents an active or completing playback session."""
    session_id: int
    data: bytes
    stop_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    is_blocking: bool = False
    error: Exception | None = None


class AudioPlaybackService:
    """
    Manages non-blocking and interruptible blocking audio playback with strict session ownership.
    Guarantees no overlapping playback sessions, supports instant non-deadlocking
    cancellation, prevents stale workers from altering active playback state, and
    rejects starting new playback if prior sessions fail to terminate.
    """

    def __init__(
        self,
        sink: AudioOutputSink | None = None,
        config: VoiceConfig | None = None,
    ) -> None:
        self.config = config or default_voice_config
        self.sink = sink or SoundDeviceOutputSink(self.config)

        self._playback_lock = threading.Lock()
        self._current_session: _PlaybackSession | None = None
        self._session_counter = 0
        self._last_error: Exception | None = None

    @property
    def is_playing(self) -> bool:
        """Return True if a playback session is currently in progress."""
        with self._playback_lock:
            return self._current_session is not None and not self._current_session.done_event.is_set()

    @property
    def _worker_thread(self) -> threading.Thread | None:
        """Backward-compatible access to current active playback thread."""
        with self._playback_lock:
            if self._current_session is not None and not self._current_session.done_event.is_set():
                return self._current_session.thread
            return None

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def play(self, pcm_data: bytes, non_blocking: bool = True) -> None:
        """
        Play raw 16-bit PCM audio.
        Guarantees that every playback session (blocking or non-blocking) is tracked.
        Prevents a new session from starting while an earlier session is still writing.
        Ensures stop() can interrupt either playback mode.
        Ensures cancellation cannot allow a stale worker/session to write after a new session starts.
        """
        if not pcm_data:
            raise ValueError("Cannot play empty audio data.")

        if len(pcm_data) % self.config.bytes_per_sample != 0:
            raise AudioConfigError(
                f"Audio byte length ({len(pcm_data)}) is not aligned to sample width ({self.config.bytes_per_sample})."
            )

        with self._playback_lock:
            # Prevent overlapping playback sessions: abort any existing playback and wait for it to exit
            if self._current_session is not None and not self._current_session.done_event.is_set():
                self._abort_session_under_lock()
                if self._current_session is not None and not self._current_session.done_event.is_set():
                    # Cancellation could not be completed safely within timeout
                    raise AudioDeviceError(
                        "Cannot start playback: previous playback session is still active and failed to cancel."
                    )

            self._session_counter += 1
            session = _PlaybackSession(
                session_id=self._session_counter,
                data=pcm_data,
                is_blocking=not non_blocking,
            )
            self._current_session = session
            self._last_error = None

            if non_blocking:
                worker = threading.Thread(
                    target=self._play_session,
                    args=(session,),
                    name=f"JarvisAudioPlaybackWorker-{session.session_id}",
                    daemon=True,
                )
                session.thread = worker
                worker.start()
                return

        # Blocking playback runs on the calling thread outside _playback_lock
        session.thread = threading.current_thread()
        self._play_session(session)

    def _play_session(self, session: _PlaybackSession) -> None:
        """Chunked playback loop bound to a specific session."""
        chunk_bytes = self.config.frame_bytes
        total_len = len(session.data)
        offset = 0

        try:
            if not self.sink.is_active:
                self.sink.open()

            while (
                offset < total_len
                and not session.stop_event.is_set()
                and self._current_session is session
            ):
                end = min(offset + chunk_bytes, total_len)
                chunk = session.data[offset:end]
                self.sink.write(chunk)
                offset = end

        except Exception as exc:
            session.error = exc
            self._last_error = exc
        finally:
            # Mark session as done writing FIRST so waiting abort callers immediately unblock
            session.done_event.set()
            with self._playback_lock:
                if self._current_session is session:
                    self._current_session = None

    def _abort_session_under_lock(self) -> None:
        """Internal abort routine called under lock."""
        session = self._current_session
        if session is None or session.done_event.is_set():
            self._current_session = None
            return

        session.stop_event.set()
        try:
            self.sink.stop()
        except Exception:
            pass

        # If the session is executing on the current thread, do not self-deadlock
        if session.thread is threading.current_thread():
            return

        session.done_event.wait(timeout=1.0)
        if session.done_event.is_set():
            if self._current_session is session:
                self._current_session = None

    def stop(self) -> None:
        """Immediately stop and abort active playback (Barge-in / Interruption)."""
        with self._playback_lock:
            self._abort_session_under_lock()

    def abort(self) -> None:
        """Alias for stop()."""
        self.stop()

    def shutdown(self) -> None:
        """Deterministic, idempotent shutdown closing output sink."""
        with self._playback_lock:
            self._abort_session_under_lock()
            try:
                self.sink.close()
            except Exception:
                pass
