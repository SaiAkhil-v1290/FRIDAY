"""
Unit tests for JARVIS Speech-to-Text (STT) Subsystem (Milestone 5.1.2).
Tests cover mocked Whisper.cpp inference, subprocess safety, input validation,
supported output formats (txt, json, stdout), malformed output handling,
multilingual preservation, temporary file lifecycle, and error handling.
"""

import json
import os
import subprocess
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
def sample_pcm_audio():
    """
    1.0 second of valid 16-bit 16kHz mono linear PCM audio (32,000 bytes).
    """
    return b"\x00\x00" * 16000


# ==============================================================================
# 1. Output Format: TXT Configuration and Parsing
# ==============================================================================

def test_txt_output_configuration_and_parsing(dummy_env, sample_pcm_audio):
    """
    Verify that format='txt' passes -otxt and parses transcription from the .txt sidecar file.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="txt",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        assert "-otxt" in cmd, "Must pass -otxt when format is txt"
        wav_path = cmd[cmd.index("-f") + 1]
        txt_path = wav_path + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Transcribed text from TXT sidecar file.")

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is True
        assert result.text == "Transcribed text from TXT sidecar file."
        assert result.duration_seconds == 1.0


def test_txt_output_missing_file(dummy_env, sample_pcm_audio):
    """
    Verify error handling when format='txt' but the .txt file was not generated.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="txt",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "txt output file was not generated" in result.error.lower()


# ==============================================================================
# 2. Output Format: JSON Configuration and Parsing
# ==============================================================================

def test_json_output_configuration_and_parsing_segments(dummy_env, sample_pcm_audio):
    """
    Verify that format='json' passes -oj and parses standard Whisper.cpp transcription segments.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        assert "-oj" in cmd, "Must pass -oj when format is json"
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        payload = {
            "result": {"language": "en"},
            "transcription": [
                {"timestamps": {"from": "00:00:00", "to": "00:00:01"}, "text": "Hello Friday,"},
                {"timestamps": {"from": "00:00:01", "to": "00:00:02"}, "text": "open the browser."},
            ],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is True
        assert result.text == "Hello Friday, open the browser."
        assert result.language == "en"
        assert result.duration_seconds == 1.0


def test_json_output_configuration_and_parsing_top_level_text(dummy_env, sample_pcm_audio):
    """
    Verify JSON parsing when Whisper output contains a top-level 'text' key.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        payload = {
            "language": "de",
            "text": "Guten Morgen JARVIS.",
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is True
        assert result.text == "Guten Morgen JARVIS."
        assert result.language == "de"


def test_json_output_missing_file(dummy_env, sample_pcm_audio):
    """
    Verify error handling when format='json' but the .json file was not created.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "json output file was not generated" in result.error.lower()


# ==============================================================================
# 3. Invalid JSON Output
# ==============================================================================

def test_invalid_json_output(dummy_env, sample_pcm_audio):
    """
    Verify explicit error when Whisper.cpp generates corrupt or unparseable JSON.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json: [truncated...")

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "invalid json in whisper output" in result.error.lower()


def test_invalid_json_structure_top_level_list(dummy_env, sample_pcm_audio):
    """
    Verify explicit error when top-level JSON is a list instead of an object.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("[\"not\", \"an\", \"object\"]")

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "top-level json must be an object" in result.error.lower()


# ==============================================================================
# 4. JSON Output Missing Expected Transcription Field
# ==============================================================================

def test_json_output_missing_expected_transcription_field(dummy_env, sample_pcm_audio):
    """
    Verify error when JSON object is missing 'text' and 'transcription' keys.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        payload = {"systeminfo": "AVX2", "params": {"model": "base"}}
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "missing expected transcription field" in result.error.lower()


def test_json_output_invalid_segment_structure(dummy_env, sample_pcm_audio):
    """
    Verify error when transcription segments are missing the 'text' key.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        payload = {
            "transcription": [
                {"timestamps": "00:00", "broken_key": 123}
            ]
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "missing valid 'text' field" in result.error.lower()


# ==============================================================================
# 5. Output Format: STDOUT Configuration and Parsing
# ==============================================================================

def test_stdout_output_configuration_and_parsing(dummy_env, sample_pcm_audio):
    """
    Verify that format='stdout' reads directly from stdout without sidecar flags or files.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Transcribed directly from stdout"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = service.transcribe(sample_pcm_audio)

        cmd = mock_run.call_args[0][0]
        assert "-otxt" not in cmd, "Must NOT pass -otxt when format is stdout"
        assert "-oj" not in cmd, "Must NOT pass -oj when format is stdout"

        assert result.success is True
        assert result.text == "Transcribed directly from stdout"
        assert result.duration_seconds == 1.0


# ==============================================================================
# 6. Malformed Non-Empty Output and Multilingual Preservation
# ==============================================================================

def test_malformed_non_empty_punctuation_only(dummy_env, sample_pcm_audio):
    """
    Verify that non-empty output consisting only of punctuation / noise symbols is rejected.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "... --- ??? !!"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "no recognizable speech content" in result.error.lower()


def test_malformed_non_empty_silence_tokens_only(dummy_env, sample_pcm_audio):
    """
    Verify that noise tokens like [BLANK_AUDIO], (music), [applause] are rejected as non-speech.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    for token in ("[BLANK_AUDIO]", "(music)", "[silence]", "[applause]", "(laughter)"):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = token
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            result = service.transcribe(sample_pcm_audio)
            assert result.success is False
            assert "no speech detected" in result.error.lower()


def test_malformed_non_empty_unprintable_characters(dummy_env, sample_pcm_audio):
    """
    Verify that output containing unprintable or binary garbage is rejected.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "\x00\x01\x02\x03\x04\x05\x06"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert ("unprintable characters" in result.error.lower() or
                "no recognizable speech content" in result.error.lower())


def test_multilingual_spoken_content_preserved(dummy_env, sample_pcm_audio):
    """
    Verify that valid multilingual spoken content (Chinese, Spanish, German, Hindi) is preserved.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    test_cases = [
        ("你好，世界", "你好，世界"),
        ("¿Cómo estás hoy?", "¿Cómo estás hoy?"),
        ("Guten Tag! Wie geht es Ihnen?", "Guten Tag! Wie geht es Ihnen?"),
        ("नमस्ते शुक्रवार", "नमस्ते शुक्रवार"),
        ("Turn off lights, please.", "Turn off lights, please."),
    ]

    for raw_input, expected in test_cases:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = raw_input
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            result = service.transcribe(sample_pcm_audio)
            assert result.success is True, f"Failed for {raw_input}: {result.error}"
            assert result.text == expected


# ==============================================================================
# 7. Empty Output for Each Supported Format
# ==============================================================================

def test_empty_output_stdout(dummy_env, sample_pcm_audio):
    """
    Verify handling of empty or whitespace-only stdout output.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="stdout",
    )
    service = SpeechToTextService(config=config)

    for empty_val in ("", "   \n\t  "):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = empty_val
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            result = service.transcribe(sample_pcm_audio)
            assert result.success is False
            assert "no speech detected" in result.error.lower()


def test_empty_output_txt(dummy_env, sample_pcm_audio):
    """
    Verify handling of empty or whitespace-only TXT output file.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="txt",
    )
    service = SpeechToTextService(config=config)

    for empty_val in ("", "   \r\n   "):
        def side_effect(cmd, **kwargs):
            wav_path = cmd[cmd.index("-f") + 1]
            txt_path = wav_path + ".txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(empty_val)
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
            return proc

        with patch("subprocess.run", side_effect=side_effect):
            result = service.transcribe(sample_pcm_audio)
            assert result.success is False
            assert "no speech detected" in result.error.lower()


def test_empty_output_json(dummy_env, sample_pcm_audio):
    """
    Verify handling of empty JSON file or empty transcription segment list.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    # 1. Literally empty JSON file
    def empty_file_side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("   \n")
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=empty_file_side_effect):
        result = service.transcribe(sample_pcm_audio)
        assert result.success is False
        assert "json output file is empty" in result.error.lower()

    # 2. JSON with empty transcription array
    def empty_segments_side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"transcription": []}, f)
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=empty_segments_side_effect):
        result = service.transcribe(sample_pcm_audio)
        assert result.success is False
        assert "no speech detected" in result.error.lower()

    # 3. JSON with whitespace-only text
    def whitespace_text_side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"text": "   \n\t "}, f)
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=whitespace_text_side_effect):
        result = service.transcribe(sample_pcm_audio)
        assert result.success is False
        assert "no speech detected" in result.error.lower()


# ==============================================================================
# 8. Temporary File Cleanup for JSON and TXT Sidecar Files
# ==============================================================================

def test_temp_file_cleanup_txt_sidecar(dummy_env, sample_pcm_audio):
    """
    Verify temporary WAV and TXT sidecar files are deleted after success and failure.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="txt",
    )
    service = SpeechToTextService(config=config)

    captured_paths = []

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        txt_path = wav_path + ".txt"
        captured_paths.append((wav_path, txt_path))
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("Clean me up!")
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)
        assert result.success is True

    wav_file, txt_file = captured_paths[0]
    assert not os.path.exists(wav_file), "WAV file must be deleted"
    assert not os.path.exists(txt_file), "TXT sidecar must be deleted"


def test_temp_file_cleanup_json_sidecar(dummy_env, sample_pcm_audio):
    """
    Verify temporary WAV and JSON sidecar files are deleted after success and failure.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        stt_output_format="json",
    )
    service = SpeechToTextService(config=config)

    captured_paths = []

    def side_effect(cmd, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        json_path = wav_path + ".json"
        captured_paths.append((wav_path, json_path))
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"text": "Clean up JSON!"}, f)
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    with patch("subprocess.run", side_effect=side_effect):
        result = service.transcribe(sample_pcm_audio)
        assert result.success is True

    wav_file, json_file = captured_paths[0]
    assert not os.path.exists(wav_file), "WAV file must be deleted"
    assert not os.path.exists(json_file), "JSON sidecar must be deleted"


# ==============================================================================
# General Lifecycle and Validation Regressions
# ==============================================================================

def test_automatic_language_configuration(dummy_env, sample_pcm_audio):
    """
    Verify that -l auto is passed to whisper-cli and auto-detected language is parsed.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        whisper_language="auto",
        stt_output_format="stdout",
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


def test_explicit_language_configuration(dummy_env, sample_pcm_audio):
    """
    Verify that an explicit language (e.g. 'de') is forwarded and reflected in result.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        whisper_language="de",
        stt_output_format="stdout",
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


def test_empty_audio_rejection(dummy_env):
    """
    Verify that empty audio bytes are rejected immediately without invoking Whisper.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
        )
    )
    with patch("subprocess.run") as mock_run:
        result = service.transcribe(b"")

        assert not mock_run.called
        assert result.success is False
        assert "empty" in result.error.lower()


def test_invalid_pcm_byte_length(dummy_env):
    """
    Verify that unaligned audio byte count (odd bytes for 16-bit PCM) is rejected.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
        )
    )
    with patch("subprocess.run") as mock_run:
        result = service.transcribe(b"\x00\x01\x02")

        assert not mock_run.called
        assert result.success is False
        assert "not aligned" in result.error.lower()


def test_audio_exceeding_max_duration(dummy_env):
    """
    Verify that audio longer than max_audio_duration_seconds is rejected.
    """
    config = VoiceConfig(
        whisper_executable_path=dummy_env["exe"],
        whisper_model_path=dummy_env["model"],
        max_audio_duration_seconds=2.0,
    )
    service = SpeechToTextService(config=config)

    oversized_audio = b"\x00\x00" * (16000 * 3)

    with patch("subprocess.run") as mock_run:
        result = service.transcribe(oversized_audio)

        assert not mock_run.called
        assert result.success is False
        assert "exceeds maximum allowed duration" in result.error


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
    result = service.transcribe(sample_pcm_audio)
    assert result.success is False
    assert "executable not found" in result.error.lower()


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
    result = service.transcribe(sample_pcm_audio)
    assert result.success is False
    assert "model file not found" in result.error.lower()


def test_process_timeout(dummy_env, sample_pcm_audio):
    """
    Verify graceful error return when the Whisper subprocess times out.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
            stt_timeout_seconds=5.0,
        )
    )
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["whisper-cli"], timeout=5.0)):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "timed out" in result.error.lower()


def test_non_zero_exit_code(dummy_env, sample_pcm_audio):
    """
    Verify handling and sanitized error reporting on non-zero exit code.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
        )
    )
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "error: failed to load model: invalid magic number"

    with patch("subprocess.run", return_value=mock_proc):
        result = service.transcribe(sample_pcm_audio)

        assert result.success is False
        assert "exited with non-zero code 1" in result.error
        assert "failed to load model" in result.error


def test_safe_subprocess_invocation_no_shell(dummy_env, sample_pcm_audio):
    """
    Verify that subprocess.run is invoked strictly with shell=False.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
            stt_output_format="stdout",
        )
    )
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Safe invocation."
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        result = service.transcribe(sample_pcm_audio)

        assert mock_run.called
        _, kwargs = mock_run.call_args
        assert kwargs.get("shell") is False, "Subprocess must strictly use shell=False"
        assert result.success is True


def test_invalid_configuration_bounds():
    """
    Verify VoiceConfig raises ValueError when STT parameters violate constraints.
    """
    with pytest.raises(ValueError, match="whisper_language"):
        VoiceConfig(whisper_language="")

    with pytest.raises(ValueError, match="stt_timeout_seconds"):
        VoiceConfig(stt_timeout_seconds=0.0)

    with pytest.raises(ValueError, match="max_audio_duration_seconds"):
        VoiceConfig(max_audio_duration_seconds=-10.0)

    with pytest.raises(ValueError, match="stt_threads"):
        VoiceConfig(stt_threads=0)

    with pytest.raises(ValueError, match="stt_output_format"):
        VoiceConfig(stt_output_format="xml")


def test_non_bytes_audio_input(dummy_env):
    """
    Verify that passing non-bytes input to transcribe returns an explicit error.
    """
    service = SpeechToTextService(
        config=VoiceConfig(
            whisper_executable_path=dummy_env["exe"],
            whisper_model_path=dummy_env["model"],
        )
    )
    result = service.transcribe("string audio")  # type: ignore
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
