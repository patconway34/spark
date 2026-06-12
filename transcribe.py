"""Voice → text via Groq Whisper. Standalone copy for Spark.

Get a free key at https://console.groq.com
Set GROQ_API_KEY in spark/.env
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
ALLOWED_LANGUAGES = {"en", "es", "english", "spanish"}


class TranscriptionUnavailable(RuntimeError):
    pass


def transcribe_audio(audio_bytes: bytes, filename: str = "memo.m4a") -> str:
    if not GROQ_API_KEY:
        raise TranscriptionUnavailable(
            "GROQ_API_KEY not set. Add it to spark/.env "
            "(get one free at https://console.groq.com)."
        )

    for attempt in range(3):
        try:
            response = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filename, audio_bytes, "application/octet-stream")},
                data={"model": GROQ_MODEL, "response_format": "verbose_json"},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            detected = (result.get("language") or "").strip().lower()
            if detected and detected not in ALLOWED_LANGUAGES:
                return ""  # background noise / non-target language — discard
            return (result.get("text") or "").strip()
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError, ConnectionError, OSError):
            if attempt == 2:
                raise
            import time
            time.sleep(0.5)
