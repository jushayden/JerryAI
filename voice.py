"""Local speech-to-text for Pocket Agent (Telegram voice messages).

Transcribes on-device with faster-whisper — no cloud, matching the local-first design.
The model loads once and is cached; the FIRST call downloads the weights (~150 MB for the
default 'base' model) and is slow, so send one voice note before a demo to warm it up.

Config (env, all optional): WHISPER_MODEL (tiny|base|small|medium|large-v3),
WHISPER_DEVICE (auto|cpu|cuda), WHISPER_COMPUTE (int8|float16|float32).
"""
import asyncio

import config

_model = None


def _load():
    """Build + cache the Whisper model. Raises RuntimeError with a hint if the lib is absent."""
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper not installed — run: pip install faster-whisper") from e
    _model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                          compute_type=config.WHISPER_COMPUTE)
    return _model


def _transcribe_sync(path: str) -> str:
    """Blocking transcription of an audio file (OGG/Opus from Telegram, etc.)."""
    model = _load()
    segments, _info = model.transcribe(str(path), vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def transcribe(path: str) -> str:
    """Transcribe an audio file to text. Never raises — returns 'Error: ...' on failure."""
    try:
        return await asyncio.to_thread(_transcribe_sync, path)
    except Exception as e:
        return f"Error: {e}"
