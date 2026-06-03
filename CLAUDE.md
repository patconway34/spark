# Spark

Voice layer for Claude Code sessions. Adds voice input (Groq Whisper) and voice output (gTTS) on top of puente's SSH terminal. Hands-free conversation with Claude Code from the phone.

## STT

**Groq only** — Groq Whisper Large v3 Turbo via `ventana/transcribe.py`. Uses `GROQ_API_KEY` from `ventana/.env`. Free tier: 2,000 req/day. Browser Web Speech API code is preserved but disabled (doesn't work on mobile).

## Architecture

Phone (browser) → voice recording → Groq Whisper STT → keystrokes into Claude's tmux session → read transcript JSONL → gTTS → audio playback

Key constraint: Claude Code must run interactively (not --print mode) to support permission prompts. So spark wraps the existing puente SSH session rather than spawning its own CLI process.

## Voice Input Flow

```
Tap mic (purple) → mic turns green (listening)
  │
  ├── Recording starts immediately (MediaRecorder, always rolling)
  ├── Volume monitor starts (Web Audio API, checks every 100ms)
  │
  │   Volume > 20 → "talking" → mic turns red (recording), silence timer cancelled
  │   Volume < 20 after talking → silence timer starts (3 seconds)
  │
  │   After 3s silence:
  │     ├── Stop current recording
  │     ├── Send audio blob to /api/transcribe (Groq Whisper)
  │     ├── Groq returns text
  │     │
  │     ├── Last word is "go ahead"? → strip "go ahead", send text to Claude via tmux
  │     └── Last word is NOT "go ahead"? → discard, keep listening
  │
  │     Start new recording for next utterance
  │
  └── Tap mic again → mic off, recording stops, volume monitor stops
```

## Trigger Word: "go ahead"

- The word **"go ahead"** at the end of speech is the send trigger
- If transcription doesn't end with "go ahead", the text is discarded and spark keeps listening
- This prevents background noise and partial sentences from being sent
- "Over" itself is stripped from the text before sending

## Voice Commands

These short phrases bypass text input and send raw keys to tmux:

| Say | Sends |
|-----|-------|
| enter | Enter |
| tab | Tab |
| control c | Ctrl+C |
| control z | Ctrl+Z |
| control d | Ctrl+D |
| escape | Escape |
| up / down / left / right | Arrow keys |
| yes | y + Enter |
| no | n + Enter |
| one / two / three | 1/2/3 + Enter |

## TTS (Voice Output)

- Disabled by default (speaker icon shows muted)
- Tap speaker icon to enable → polls `/api/latest` every 1s
- Reads the latest assistant message from the Claude transcript JSONL
- Long responses (>300 chars) get summarized by mente before speaking
- Stop button appears during playback — tap to silence
- During TTS playback, volume monitor pauses (so it doesn't hear itself)

## Button Controls

Two rows of key buttons (Esc, Tab, Enter, ^C, ^Z, ^D, arrows, Yes, No) plus number row (1, 2, 3). Bottom bar: stop button (red, hidden until speaking), mic button (purple/green/red), speaker toggle.

## Files

- `app.py` — Flask server (port 5023). Routes: `/`, `/api/key`, `/api/voice-text`, `/api/transcribe`, `/api/latest`, `/api/tts`
- `templates/chat.html` — full UI: terminal iframe + controls + all JS voice logic
- `static/style.css` — chat-style CSS (currently unused, styles are inline in chat.html)
- `last_response.txt` — latest chat transcript dump
- `chat_summary.txt` — running summary of voice conversation

## Dependencies

- puente (ttyd terminal at terminal.tradingdata.net)
- ventana/transcribe.py (Groq Whisper)
- mente/simple.py (summarization for long responses)
- gTTS (text-to-speech)

## Story

2026-05-31 — Documented full voice flow, trigger word, volume detection, and command map.
2026-05-30 — Added dual STT: Browser (Web Speech API, always-on with "go" trigger) and Groq Whisper (push-to-talk via ventana/transcribe.py). Browser STT disabled (doesn't work on mobile). Cleaned up: removed hook-based prototype, pivoted to terminal-wrapping via puente's SSH session.
2026-05-29 — Project created. Hook-based prototype built and tested. gTTS + Telegram delivery confirmed working. Hooks proved unreliable for real-time voice.
