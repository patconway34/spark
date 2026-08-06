# Spark

**A voice-controlled mobile front end for [Claude Code](https://claude.com/claude-code).**
Talk to a coding agent from your phone — dictate prompts, drive the terminal
with a game controller, and have replies read back to you — while the actual
work runs on your desktop.

<!-- screenshot: templates render a 3-tab terminal view; drop a phone
     screenshot here as docs/screenshot.png and link it -->

---

## The problem it solves

Claude Code is a terminal app. Terminals are miserable on a phone: tiny text, no
usable keyboard, and any SSH client drops the session the moment the screen
sleeps.

Spark keeps the session on the desktop, where it is stable, and puts a
**purpose-built mobile control surface** in front of it. Sessions live in tmux
and survive disconnects, reboots of the browser, and losing signal entirely.
The phone is a remote control, not a host.

## What it does

- **Voice input** — hold to talk, transcribed by Groq Whisper, typed into the
  live session
- **Read-back** — text-to-speech of the agent's reply, so you can listen instead
  of squint
- **Three terminals**, all kept loaded so switching is instant
- **Game controller support** — an 8BitDo Micro in keyboard mode drives mic,
  Enter, Escape, scrolling and session switching, so the phone can stay in a
  pocket or a mount
- **Screen capture and SMS/summary hooks** for reading back state when you are
  away from the machine

## Architecture

```
  Phone (browser)
        |
        |  HTTPS, Cloudflare tunnel + Access
        v
  Flask app  (Windows, :5023)  ──►  ttyd  (:7682-7684)  ──►  tmux ──► Claude Code
        |                              (read-only terminal view)
        └──►  tmux_helper.py  (persistent, inside WSL)
                 all keystrokes / scroll / capture
```

Two details worth calling out:

**Input never goes through the terminal.** ttyd runs read-only; every keystroke
is routed through the Flask API and injected with `tmux send-keys`. A writable
ttyd auto-focuses its textarea, which pops the Android soft keyboard on every
tab switch. Read-only means the keyboard appears only when asked for.

**The app runs on Windows; tmux runs in WSL.** That boundary is the single
biggest performance factor in the system — see below.

## Engineering notes

### Crossing the WSL boundary costs ~130ms, every time

Each `wsl -e tmux ...` invocation measured ~130ms. The tell was that `wsl -e
true` — a command that does nothing — measured exactly the same. The cost is
Windows→WSL process launch, not tmux.

That toll was being paid on every keystroke, every scroll (2-3× per press) and
every session poll. It never looked like a bug, just a system that felt
sluggish.

The fix (`tmux_helper.py`) keeps **one** Python process alive inside WSL and
talks to it over stdin/stdout, one JSON object per line:

| | before | after |
|---|---|---|
| Session poll | ~130-150 ms | **~5 ms** |
| Single tmux call | ~130 ms | **~2 ms** |
| Text + Enter | ~260 ms (2 spawns) | **~3.6 ms** (1 round trip) |

A pipe rather than a TCP socket, deliberately: no listening port to secure, no
dependency on WSL2 localhost-forwarding (which differs between NAT and mirrored
networking modes), and the helper exits on its own when Spark does — its stdin
hits EOF.

**Every failure path falls back to the original spawn.** If the helper dies,
the next request respawns it; if it cannot start at all, calls revert to `wsl -e
tmux` at the old speed, and a cooldown stops a broken helper being retried on
every request. Verified by killing the helper mid-flight and by deleting it
outright — both degrade rather than break. Worst case is slow, never dead.

### A bug the tests found

tmux's argv lexer strips an unescaped trailing `;` off a word and treats it as a
command separator — so `ls -la;` silently arrived as `ls -la`. It had been that
way in the original spawn path too. `test_tmux_paths.py` covers it along with
unicode, backslashes, quoting and oversized text, across both send paths.

```bash
python test_tmux_paths.py     # builds a throwaway session; safe against a live Spark
```

### `gotchas/` — the interesting part

Every non-obvious failure in this project has a writeup: why mobile Chrome
renders a blank terminal behind Basic Auth, why four same-origin iframes crash
the Android renderer, why `wsl tmux` eats `#{...}` format strings (bash treats
`#` as a comment), why PageUp shows a splash screen instead of scrollback.

They exist because most of these cost hours and are completely invisible in the
code afterward. See [`gotchas/README.md`](gotchas/README.md).

### Debugging hardware you cannot instrument

The controller talks to Spark as a keyboard, so "which button sends what" is
guesswork — and saved screenshots of the mapping app go stale. Spark has a key
debug mode that captures every keypress, echoes it on screen and logs it, and
suppresses all other handling.

Enable it by creating a `.keydebug` file and reloading; there is no restart and
no code edit. It is a file flag rather than a URL parameter because the phone
opens Spark from a home-screen PWA shortcut with a fixed URL, where query
parameters never survive.

It paid for itself immediately: a button reported as broken turned out to be
sending **no keyboard event at all**, which localised the fault to the
controller rather than the app — Android silently swallows media and volume
keys before any web page sees them.

## Security model

Spark injects keystrokes into live terminals, so exposure is the whole risk.

- Every request must carry `SPARK_TOKEN` — query param on first visit, then a
  cookie, or an `X-Spark-Token` header
- Behind a Cloudflare tunnel with Access in front of the hostname
- ttyd binds only to loopback from the tunnel's perspective and is never exposed
  directly to the network
- No secrets in the repo: everything comes from `.env` (gitignored).
  See [`.env.example`](.env.example)

## Setup

**Requirements:** Python 3.10+, tmux, [ttyd](https://github.com/tsl0922/ttyd),
[Claude Code](https://claude.com/claude-code), and a free
[Groq](https://console.groq.com) API key for speech-to-text.

```bash
pip install -r requirements.txt
cp .env.example .env        # add GROQ_API_KEY and SPARK_TOKEN
claude /login               # uses a Claude subscription; no API key needed

./start.sh                  # creates tmux sessions + launches ttyd per session
python app.py               # Spark on :5023
```

`start.sh` is idempotent — existing tmux sessions are left alone, so running it
again never disturbs a conversation in progress.

For remote access, point a Cloudflare tunnel at `:5023`, plus one route per ttyd
port, and set `SPARK_PUBLIC_HOST` in `.env`.

## Layout

| Path | Purpose |
|---|---|
| `app.py` | Flask server — auth, routing, voice, tmux channel |
| `tmux_helper.py` | Persistent tmux runner, lives inside WSL |
| `test_tmux_paths.py` | Functional tests for both send paths |
| `transcribe.py` | Groq Whisper speech-to-text |
| `summarize.py` | Summarisation for read-back |
| `templates/chat.html` | The mobile UI |
| `start.sh` | tmux session + ttyd launcher |
| `theme.json` | Colors, hot-reloaded |
| `terminal_names.txt` | Tab names, hot-reloaded |
| `gotchas/` | Writeups of every non-obvious failure |
| `Button Map and Procedure/` | Controller mapping + probe procedure |
