# Spark

Voice layer for Claude Code sessions. Adds voice input (Groq Whisper) and voice output (gTTS) on top of puente's SSH terminal. Hands-free conversation with Claude Code from the phone.

## STT

**Groq only** — Groq Whisper Large v3 Turbo via `ventana/transcribe.py`. Uses `GROQ_API_KEY` from `ventana/.env`. Free tier: 2,000 req/day.

## Architecture

Phone (browser) → push-to-talk recording → Groq Whisper STT → keystrokes into Claude's tmux session.

Key constraint: Claude Code must run interactively (not --print mode) to support permission prompts. So spark wraps the existing puente SSH session rather than spawning its own CLI process.

## Voice Input Flow — Push-to-Talk

```
Tap MIC (or Bluetooth clicker) → button turns red, recording starts
  │
  └── Tap again → recording stops → audio sent to Groq → transcript sent to Claude
```

No trigger words, no volume detection, no silence timers. Pure manual control.

Bluetooth clicker support: pairs as `volumeup` keypress, JS listener catches it and toggles mic.

## Button Controls

Row 1: Hamburger (utility keys) | PgUp | PgDn | Session tabs
Row 2: ESC | MIC | ENTER

Hamburger contains: /new, Tab, Shift-Tab, Up, Down, ^C, ^Z, Screenshot.

## Files

- `app.py` — Flask server (port 5023). Routes: `/`, `/api/key`, `/api/voice-text`, `/api/transcribe`, `/api/latest`, `/api/tts`
- `templates/chat.html` — full UI: terminal iframe + controls + all JS voice logic
- `static/style.css` — chat-style CSS (currently unused, styles are inline in chat.html)
- `last_response.txt` — latest chat transcript dump
- `chat_summary.txt` — running summary of voice conversation

## Dependencies

- tmux + ttyd (terminal multiplexer + web terminal)
- Groq Whisper API (speech-to-text via transcribe.py)
- Claude Agent SDK (optional — summarization for long responses)
- gTTS / edge-tts (text-to-speech)

## Story

2026-06-24 — Push-to-talk rewrite. Ripped out volume monitoring, clip recording, keyword scanning, silence detection, phantom filtering, "go ahead" trigger. Replaced with simple toggle: tap MIC → record → tap again → transcribe → send. Added Bluetooth clicker support (volumeup key). Mic button moved to main row (ESC | MIC | ENTER). ~400 lines of voice JS reduced to ~50.
2026-06-13 — Stabilization: cut from 6 sessions to 3 (Dev, Alpha, Bravo). Killed the background feed loop that was scanning all panes every 5s and calling mente for AI summaries — this was the CPU hog causing fan spin. Removed /talk command center, /api/feed, /api/send, /api/questions, question queue. Spark is now lobby + chat with 3 tabs, no background threads.
2026-06-10 — /talk became the command center: six status cards. Background feed thread scans all panes every 5s (/api/feed).
2026-06-10 — Multi-session question queue on /talk.
2026-05-31 — Documented full voice flow, trigger word, volume detection, and command map.
2026-05-30 — Added dual STT: Browser (Web Speech API, always-on with "go" trigger) and Groq Whisper (push-to-talk via ventana/transcribe.py). Browser STT disabled (doesn't work on mobile). Cleaned up: removed hook-based prototype, pivoted to terminal-wrapping via puente's SSH session.
2026-05-29 — Project created. Hook-based prototype built and tested. gTTS + Telegram delivery confirmed working. Hooks proved unreliable for real-time voice.
