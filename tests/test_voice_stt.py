"""
Unit tests for JARVIS Speech-to-Text (STT) Subsystem (Milestone 5.1.2).
Tests cover mocked Whisper.cpp inference, subprocess safety, input validation,
temporary file lifecycle, error handling, and configuration integrity.
"""

import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from voice.config import VoiceConfig
from voice.stt import SpeechToTextService, TranscriptionResult, SpeechToTextError


@pytest.fixture
def dummy_env(tmp_path):
    """
    Creates temporary dummy executable and model files for testing availability.
    """
    fake_exe = tmp_path / "whisper-cli.exe"
    fake_exe.write_text("#!/bin/sh\necho fake", encoding="utf-8")
    
    fake_model = tmp_path / "ggml-base.bin"
    fake_model.write_bytes(b"\x00" * 1024)

    return {
        "exe": str(fake_exe),
        "model": str(fake_model),
        "dir": tmp_path,
    }


@pytest.fixture
def stt_service(dummy_env):
    """
    SpeechToTextService configured with dummy local paths.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        whisper_language="auto",
        stt_timeout_seconds=5.0,
        max_audio_duration_seconds=10.0,
        stt_threads=2,
    )
    return SpeechToTextService(config=config)


@pytest.fixture
def sample_pcm_audio():
    """
    1.0 second of valid 16-bit 16kHz mono linear PCM audio (32,000 bytes).
    """
    return b"\x00\x00" * 16000


# ==============================================================================
# 1. Successful Transcription
# ==============================================================================

def test_successful_transcription_from_stdout(stt_service, sample_pcm_audio):
    """
    Verify successful transcription when text is delivered via subprocess stdout.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Hello Friday, open the browser."
    mock_proc.stderr = "whisper_full_with_state: auto-detected language: en (p = 0.98)"

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = stt_service.transcribe(sample_pcm_audio)

        assert mock_run.called
        assert result.success is True
        assert result.text == "Hello Friday, open the browser."
        assert result.duration_seconds == 1.0
        assert result.language == "en"
        assert result.error is None


def test_successful_transcription_from_txt_file(stt_service, sample_pcm_audio):
    """
    Verify successful transcription when text is read from the generated .txt file.
    """
    def side_effect(cmd, **kwargs):
        # Find the wav path from cmd [-f, <wav_path>]
        wav_idx = cmd.index("-f") + 1
        wav_path = cmd[wav_idx]
        txt_path = wav_path + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Transcribed message from sidecar file.")
        
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is True
        assert result.text == "Transcribed message from sidecar file."
        assert result.duration_seconds == 1.0


# ==============================================================================
# 2. Automatic Language Configuration
# ==============================================================================

def test_automatic_language_configuration(dummy_env, sample_pcm_audio):
    """
    Verify that -l auto is passed to whisper-cli and auto-detected language is parsed.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        whisper_language="auto",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Bonjour le monde"
    mock_proc.stderr = "whisper_full_with_state: auto-detected language: fr (p = 0.992)"

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = service.transcribe(sample_pcm_audio)

        cmd = mock_run.call_args[0][0]
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "auto"

        assert result.success is True
        assert result.language == "fr"
        assert result.text == "Bonjour le monde"


# ==============================================================================
# 3. Explicit Language Configuration
# ==============================================================================

def test_explicit_language_configuration(dummy_env, sample_pcm_audio):
    """
    Verify that an explicit language (e.g. 'de') is forwarded and reflected in result.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        whisper_language="de",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Guten Morgen"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = service.transcribe(sample_pcm_audio)

        cmd = mock_run.call_args[0][0]
        assert "-l" in cmd
        assert cmd[cmd.index("-l") + 1] == "de"

        assert result.success is True
        assert result.language == "de"
        assert result.text == "Guten Morgen"


# ==============================================================================
# 4. Empty Audio
# ==============================================================================

def test_empty_audio_rejection(stt_service):
    """
    Verify that empty audio bytes are rejected immediately without invoking Whisper.
    """
    with patch("subprocess.run") as mock_run:
        result = stt_service.transcribe(b"")

        assert not mock_run.called
        assert result.success is False
        assert result.text == ""
        assert "empty" in result.error.lower()
        assert result.duration_seconds == 0.0


# ==============================================================================
# 5. Invalid PCM Byte Length
# ==============================================================================

def test_invalid_pcm_byte_length(stt_service):
    """
    Verify that unaligned audio byte count (odd bytes for 16-bit PCM) is rejected.
    """
    odd_bytes = b"\x00\x01\x02"  # 3 bytes, unaligned with 2-byte samples

    with patch("subprocess.run") as mock_run:
        result = stt_service.transcribe(odd_bytes)

        assert not mock_run.called
        assert result.success is False
        assert "not aligned" in result.error.lower()
        assert result.duration_seconds == 0.0


# ==============================================================================
# 6. Audio Exceeding Maximum Duration
# ==============================================================================

def test_audio_exceeding_max_duration(dummy_env):
    """
    Verify that audio longer than max_audio_duration_seconds is rejected.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        max_audio_duration_seconds=2.0,  # Max 2.0s = 64,000 bytes
    )
    service = SpeechToTextService(config=config)

    oversized_audio = b"\x00\x00" * (16000 * 3)  # 3.0s = 96,000 bytes

    with patch("subprocess.run") as mock_run:
        result = service.transcribe(oversized_audio)

        assert not mock_run.called
        assert result.success is False
        assert "exceeds maximum allowed duration" in result.error
        assert result.duration_seconds == 3.0


# ==============================================================================
# 7. Missing Whisper Executable
# ==============================================================================

def test_missing_whisper_executable(dummy_env, sample_pcm_audio):
    """
    Verify error handling when the Whisper executable path does not exist.
    """
    config = VoiceConfig(
        whisper_executable_path=str(dummy_env["dir"] / "nonexistent_whisper.exe"),
        whisper_model_path=dummy_env["model"],
    )
    service = SpeechToTextService(config=config)

    assert service.is_available() is False

    with patch("subprocess.run") as mock_run:
        result = service.transcribe(sample_pcm_audio)

        assert not mock_run.called
        assert result.success is False
        assert "executable not found" in result.error.lower()


def test_unconfigured_whisper_executable(dummy_env, sample_pcm_audio):
    """
    Verify error handling when executable path is None or empty.
    """
    config = VoiceConfig(
        whisper_executable_path=None,
        whisper_model_path=dummy_env["model"],
    )
    service = SpeechToTextService(config=config)

    assert service.is_available() is False
    result = service.transcribe(sample_pcm_audio)
    assert result.success is False
    assert "not configured" in result.error.lower()


# ==============================================================================
# 8. Missing Model File
# ==============================================================================

def test_missing_model_file(dummy_env, sample_pcm_audio):
    """
    Verify error handling when the model file path does not exist.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=str(dummy_env["dir"] / "nonexistent_model.bin"),
    )
    service = SpeechToTextService(config=config)

    assert service.is_available() is False

    with patch("subprocess.run") as mock_run:
        result = service.transcribe(sample_pcm_audio)

        assert not mock_run.called
        assert result.success is False
        assert "model file not found" in result.error.lower()


def test_unconfigured_model_file(dummy_env, sample_pcm_audio):
    """
    Verify error handling when model path is None.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=None,
    )
    service = SpeechToTextService(config=config)

    assert service.is_available() is False
    result = service.transcribe(sample_pcm_audio)
    assert result.success is False
    assert "not configured" in result.error.lower()


# ==============================================================================
# 9. Process Timeout
# ==============================================================================

def test_process_timeout(stt_service, sample_pcm_audio):
    """
    Verify graceful error return when the Whisper subprocess times out.
    """
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["whisper-cli"], timeout=5.0)):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "timed out" in result.error.lower()
        assert result.text == ""
        assert result.duration_seconds == 1.0


# ==============================================================================
# 10. Non-zero Process Exit Code
# ==============================================================================

def test_non_zero_exit_code(stt_service, sample_pcm_audio):
    """
    Verify handling and sanitized error reporting on non-zero exit code.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "error: failed to load model: invalid magic number"

    with patch("subprocess.run", return_value=mock_proc):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "exited with non-zero code 1" in result.error
        assert "failed to load model" in result.error
        assert result.text == ""


# ==============================================================================
# 11. Empty or Whitespace Transcription Output
# ==============================================================================

def test_empty_transcription_output(stt_service, sample_pcm_audio):
    """
    Verify that an empty string output is treated as a non-successful result.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert result.text == ""
        assert "no speech detected" in result.error.lower()


def test_whitespace_only_transcription_output(stt_service, sample_pcm_audio):
    """
    Verify that whitespace-only output is treated as non-successful.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "   \n\t   \r\n"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert result.text == ""
        assert "no speech detected" in result.error.lower()


def test_blank_audio_marker_output(stt_service, sample_pcm_audio):
    """
    Verify that Whisper's [BLANK_AUDIO] marker is stripped and treated as no speech.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "[BLANK_AUDIO]"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert result.text == ""
        assert "no speech detected" in result.error.lower()


# ==============================================================================
# 12. Malformed Output and Timestamp Cleaning
# ==============================================================================

def test_malformed_output_timestamp_stripping(stt_service, sample_pcm_audio):
    """
    Verify that timestamp markers like [00:00:00.000 --> 00:00:02.000] are cleaned.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "[00:00:00.000 --> 00:00:01.000]   Turn on living room light."
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = stt_service.transcribe(sample_pcm_audio)

        assert result.success is True
        assert result.text == "Turn on living room light."


# ==============================================================================
# 13. Temporary File Cleanup After Success
# ==============================================================================

def test_temp_file_cleanup_after_success(stt_service, sample_pcm_audio):
    """
    Verify temporary WAV and sidecar text files are removed after successful transcription.
    """
    captured_wav_path = []

    def mock_subprocess(cmd, **kwargs):
        wav_idx = cmd.index("-f") + 1
        wav_path = cmd[wav_idx]
        captured_wav_path.append(wav_path)
        assert os.path.isfile(wav_path), "Temp WAV must exist during execution"

        # Simulate whisper creating sidecar txt
        sidecar_txt = wav_path + ".txt"
        with open(sidecar_txt, "w", encoding="utf-8") as f:
            f.write("Success")

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=mock_subprocess):
        result = stt_service.transcribe(sample_pcm_audio)
        assert result.success is True

    assert len(captured_wav_path) == 1
    wav_file = captured_wav_path[0]
    sidecar_txt = wav_file + ".txt"

    assert not os.path.exists(wav_file), "Temp WAV file must be deleted after success"
    assert not os.path.exists(sidecar_txt), "Sidecar text file must be deleted after success"


# ==============================================================================
# 14. Temporary File Cleanup After Failure
# ==============================================================================

def test_temp_file_cleanup_after_failure(stt_service, sample_pcm_audio):
    """
    Verify temporary WAV file is removed when subprocess execution fails.
    """
    captured_wav_path = []

    def mock_subprocess_fail(cmd, **kwargs):
        wav_idx = cmd.index("-f") + 1
        wav_path = cmd[wav_idx]
        captured_wav_path.append(wav_path)
        assert os.path.isfile(wav_path), "Temp WAV must exist during execution"
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    with patch("subprocess.run", side_effect=mock_subprocess_fail):
        result = stt_service.transcribe(sample_pcm_audio)
        assert result.success is False

    assert len(captured_wav_path) == 1
    wav_file = captured_wav_path[0]
    assert not os.path.exists(wav_file), "Temp WAV file must be deleted after timeout failure"


def test_temp_file_cleanup_failure_resilience(stt_service, sample_pcm_audio):
    """
    Verify that an OSError during temporary file cleanup does not crash the service.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Everything worked."
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        with patch("os.unlink", side_effect=OSError("Access denied during cleanup")):
            result = stt_service.transcribe(sample_pcm_audio)

            # Result is still returned successfully without raising an unhandled exception
            assert result.success is True
            assert result.text == "Everything worked."


# ==============================================================================
# 15. Safe Subprocess Invocation Without shell=True
# ==============================================================================

def test_safe_subprocess_invocation_no_shell(stt_service, sample_pcm_audio):
    """
    Verify that subprocess.run is invoked strictly with shell=False.
    """
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Safe invocation."
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = stt_service.transcribe(sample_pcm_audio)

        assert mock_run.called
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell") is False, "Subprocess must strictly use shell=False"
        assert result.success is True


# ==============================================================================
# 16. Configuration Validation
# ==============================================================================

def test_invalid_configuration_bounds():
    """
    Verify VoiceConfig raises ValueError when STT parameters violate constraints.
    """
    # Empty language
    with pytest.raises(ValueError, match="whisper_language"):
        VoiceConfig(whisper_language="")

    with pytest.raises(ValueError, match="whisper_language"):
        VoiceConfig(whisper_language="   ")

    # Non-positive timeout
    with pytest.raises(ValueError, match="stt_timeout_seconds"):
        VoiceConfig(stt_timeout_seconds=0.0)

    with pytest.raises(ValueError, match="stt_timeout_seconds"):
        VoiceConfig(stt_timeout_seconds=-1.0)

    # Non-positive max audio duration
    with pytest.raises(ValueError, match="max_audio_duration_seconds"):
        VoiceConfig(max_audio_duration_seconds=0.0)

    with pytest.raises(ValueError, match="max_audio_duration_seconds"):
        VoiceConfig(max_audio_duration_seconds=-10.0)

    # Non-positive thread count
    with pytest.raises(ValueError, match="stt_threads"):
        VoiceConfig(stt_threads=0)

    # Invalid output format
    with pytest.raises(ValueError, match="stt_output_format"):
        VoiceConfig(stt_output_format="mp3")


def test_non_bytes_audio_input(stt_service):
    """
    Verify that passing non-bytes input to transcribe returns an explicit error.
    """
    result = stt_service.transcribe("string audio")  # type: ignore
    assert result.success is False
    assert "must be bytes" in result.error


def test_incompatible_sample_rate_for_whisper(dummy_env, sample_pcm_audio):
    """
    Verify that an audio sample rate other than 16000 is rejected by transcribe.
    """
    config = VoiceConfig(
        sample_rate=8000,
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
    )
    service = SpeechToTextService(config=config)
    result = service.transcribe(sample_pcm_audio)

    assert result.success is False
    assert "Unsupported audio format for Whisper" in result.error
