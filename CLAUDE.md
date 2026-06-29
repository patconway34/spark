# Spark

Voice layer for Claude Code sessions. Adds voice input (Groq Whisper) and voice output (gTTS) on top of puente's SSH terminal. Hands-free conversation with Claude Code from the phone.

## STT

**Groq only** — Groq Whisper Large v3 Turbo via `ventana/transcribe.py`. Uses `GROQ_API_KEY` from `ventana/.env`. Free tier: 2,000 req/day.

## Architecture

Phone (browser) → push-to-talk recording → Groq Whisper STT → keystrokes into Claude's tmux session.

Key constraint: Claude Code must run interactively (not --print mode) to support permission prompts. So spark wraps the existing puente SSH session rather than spawning its own CLI process.

## Voice Input Flow — Push-to-Talk

```
Tap MIC (or hotkey) → button turns red, recording starts
  │
  └── Tap again → recording stops → audio sent to Groq → transcript sent to Claude
```

No trigger words, no volume detection, no silence timers. Pure manual control.

## Hotkeys — Mic Toggle

| Trigger | How | Works on phone? |
|---------|-----|----------------|
| Ctrl+M | Desktop keyboard | No (phone has no Ctrl) |
| F9 | AutoHotkey remap on Windows | No (desktop only) |
| Double-tap PageDown | Presentation clicker (Logitech R400, cheap clones) | YES |
| Double-tap PageUp | Presentation clicker | YES |
| Tap MIC button | On-screen | YES |

### Bluetooth on Android Chrome — What Actually Works

Volume keys (`AudioVolumeUp/Down`) are **intercepted by Android OS** and never reach JavaScript. This is why the old clicker code failed. Keys that DO reach JS from Bluetooth HID devices:

| Key | Reaches JS? | Sent by |
|-----|-------------|---------|
| Enter | YES | AB Shutter 3 "Android" button |
| PageDown / PageUp | YES | Presentation clickers |
| Space, F-keys, Arrows | YES | Bluetooth keyboards |
| Volume Up/Down | **NO** | OS eats them |
| Media keys | Only via Media Session API | Headset buttons |

Double-tap window: 400ms. Single taps pass through normally.

## Button Controls

Row 1: Hamburger (utility keys) | PgUp | PgDn | Session tabs
Row 2: Type bar | SEND
Row 3: ESC | MIC | ENTER

Hamburger contains: Yes, No, /new, /compact, Tab, ⇧Tab, ▲, ▼, ^C, ^Z, 1, 2, 3, 📷 Save, 📋 Copy, 📲 Text, 🔊 Play.

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

2026-06-29 — Stability & hotkey overhaul. Fixed session race condition: all API calls (key, paste, scroll, voice-text) now send explicit session ID instead of relying on server-side global state. Tab switching awaits backend confirmation before updating UI. Added error toasts for failed key presses. Discovered why Bluetooth clicker never worked: Android OS intercepts volume keys before they reach the browser JS. Replaced with double-tap PageDown/PageUp detection (400ms window) for presentation clickers. Mic transcriptions now paste directly into terminal without Enter — user accumulates multiple transcriptions and hits Enter when ready.
2026-06-24 — Push-to-talk rewrite. Ripped out volume monitoring, clip recording, keyword scanning, silence detection, phantom filtering, "go ahead" trigger. Replaced with simple toggle: tap MIC → record → tap again → transcribe → send. Mic button moved to main row (ESC | MIC | ENTER). ~400 lines of voice JS reduced to ~50.
2026-06-13 — Stabilization: cut from 6 sessions to 3 (Dev, Alpha, Bravo). Killed the background feed loop that was scanning all panes every 5s and calling mente for AI summaries — this was the CPU hog causing fan spin. Removed /talk command center, /api/feed, /api/send, /api/questions, question queue. Spark is now lobby + chat with 3 tabs, no background threads.
2026-06-10 — /talk became the command center: six status cards. Background feed thread scans all panes every 5s (/api/feed).
2026-06-10 — Multi-session question queue on /talk.
2026-05-31 — Documented full voice flow, trigger word, volume detection, and command map.
2026-05-30 — Added dual STT: Browser (Web Speech API, always-on with "go" trigger) and Groq Whisper (push-to-talk via ventana/transcribe.py). Browser STT disabled (doesn't work on mobile). Cleaned up: removed hook-based prototype, pivoted to terminal-wrapping via puente's SSH session.
2026-05-29 — Project created. Hook-based prototype built and tested. gTTS + Telegram delivery confirmed working. Hooks proved unreliable for real-time voice.
