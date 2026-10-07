"""Spark — voice layer for Claude Code via tmux/ttyd.

Embeds ttyd terminal in an iframe. Sends voice input (Groq Whisper STT)
as keystrokes into tmux sessions.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import platform

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, send_file, send_from_directory

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from transcribe import transcribe_audio

# Windows Python for subprocess calls (mente, buzz, yarn)
_WIN_PYTHON = r"C:\Users\Patrick\miniconda3\python.exe" if platform.system() == "Windows" else "/mnt/c/Users/Patrick/miniconda3/python.exe"

# --- App setup ---

app = Flask(__name__)
app.secret_key = os.getenv("SPARK_SECRET_KEY") or hashlib.sha256(
    (str(Path(__file__).resolve().parent) + "spark-fallback").encode()
).hexdigest()

# --- Auth ---
# Spark is reachable from the public internet through a Cloudflare tunnel, and
# its API injects keystrokes into live terminals. Every request must carry
# SPARK_TOKEN, either as ?token=... (first visit — sets a cookie), the cookie,
# or an X-Spark-Token header.

SPARK_TOKEN = os.getenv("SPARK_TOKEN", "")

# The hostname the tunnel serves Spark on. Config, not a secret — but it is
# deployment-specific, so it lives in .env rather than in the source.
PUBLIC_HOST = os.getenv("SPARK_PUBLIC_HOST", "")


def _token_ok():
    supplied = (request.args.get("token")
                or request.cookies.get("spark_token")
                or request.headers.get("X-Spark-Token") or "")
    return bool(SPARK_TOKEN) and hmac.compare_digest(supplied, SPARK_TOKEN)


def _is_local_direct():
    """True for genuine localhost requests. Tunnel traffic also arrives from
    127.0.0.1 (cloudflared runs locally) but always carries CF headers."""
    return (request.remote_addr == "127.0.0.1"
            and "CF-Connecting-IP" not in request.headers)


@app.before_request
def _auth_guard():
    if not SPARK_TOKEN:
        return  # no token configured — auth disabled (warned at startup)
    if request.path.startswith("/static/"):
        return
    # PWA plumbing is exempt, and MUST be. Chrome fetches a web manifest with
    # credentials OMITTED — it deliberately withholds the cookie — so a gated
    # manifest returns 401, Chrome fails to parse it as JSON, and the only
    # symptom is "this app cannot be installed" with no further explanation.
    # Neither file carries secrets: the manifest is public app metadata and
    # sw.js is a no-op stub. The terminals and the API stay gated as before.
    if request.path in ("/manifest.webmanifest", "/sw.js", "/offline.html"):
        return
    if _is_local_direct() or _token_ok():
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "Unauthorized"}), 401
    return "Unauthorized — open with ?token=YOUR_SPARK_TOKEN", 401


@app.after_request
def _set_token_cookie(resp):
    # First visit with ?token=... — persist it as a cookie for a year
    if SPARK_TOKEN and request.args.get("token") == SPARK_TOKEN:
        resp.set_cookie("spark_token", SPARK_TOKEN, max_age=31536000,
                        httponly=True, samesite="Lax")
    return resp


# Auto-detect platform: on Windows, tmux runs via WSL; on Linux, directly.
# GOTCHA (see gotchas/wsl-tmux-format-strings.md): plain `wsl tmux ...` runs the
# command through bash, where `#` starts a comment — so tmux format strings like
# #{alternate_on} get silently truncated and display-message returns the status
# line instead. `wsl -e` execs tmux directly (no shell), preserving argv exactly.
_IS_WINDOWS = platform.system() == "Windows"
_TMUX_PREFIX = ["wsl", "-e", "tmux"] if _IS_WINDOWS else ["tmux"]
# tmux + Claude's transcripts both live inside WSL, so the retrieval engine runs there.
_PY_PREFIX = ["wsl", "-e", "/usr/bin/python3"] if _IS_WINDOWS else ["/usr/bin/python3"]


def _tmux_cmd(*args):
    """Build a tmux command list, adding 'wsl' prefix on Windows."""
    return _TMUX_PREFIX + list(args)


# --- Persistent tmux channel ------------------------------------------------
# Every `wsl -e tmux ...` spawn costs ~130ms of interop overhead. That is the
# process launch, not tmux: `wsl -e true`, which does nothing at all, measures
# the same 130ms. Spark was paying it on every keystroke, every scroll (2-3x)
# and every 10s session poll.
#
# _TmuxChannel keeps ONE python3 helper alive inside WSL (tmux_helper.py) and
# talks to it over stdin/stdout — ~2ms per round trip, and a batch of commands
# costs one round trip instead of N.
#
# Every failure path falls back to the original per-call spawn, so the worst
# case is the old speed rather than a dead terminal.

_HELPER_WSL_PATH = "/mnt/c/dev/spark/tmux_helper.py"
_HELPER_COOLDOWN = 30  # seconds to stop retrying a helper that won't start


class _TmuxResult:
    """Mimics the subprocess.CompletedProcess fields the call sites use."""
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _TmuxChannel:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._seq = 0
        self._mode = None          # last logged mode, so we log only transitions
        self._cooldown_until = 0.0

    def _spawn(self):
        self._proc = subprocess.Popen(
            _PY_PREFIX + [_HELPER_WSL_PATH],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1,
        )

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def _transact(self, cmds):
        """One request/response. Raises on any pipe or protocol trouble."""
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()
        self._seq += 1
        req_id = self._seq
        self._proc.stdin.write(json.dumps({"id": req_id, "cmds": cmds}) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise IOError("helper closed the pipe")
        resp = json.loads(line)
        if resp.get("id") != req_id:
            # Reply out of step with the request — the stream is desynced and
            # every later read would be off by one. Restart rather than guess.
            raise IOError(f"helper id {resp.get('id')} != {req_id}")
        if resp.get("error"):
            raise IOError(resp["error"])
        return resp.get("results") or []

    def run(self, cmds):
        """Run argv lists in one round trip. Returns results, or None to fall back."""
        if time.time() < self._cooldown_until:
            return None
        with self._lock:
            for attempt in (1, 2):  # one free retry, to respawn a dead helper
                try:
                    results = self._transact(cmds)
                    if self._mode != "helper":
                        logging.info("TMUX via persistent helper (~2ms/call)")
                        self._mode = "helper"
                    return results
                except Exception as e:
                    self._kill()
                    if attempt == 2:
                        self._cooldown_until = time.time() + _HELPER_COOLDOWN
                        if self._mode != "fallback":
                            logging.warning(
                                f"TMUX helper unavailable ({e}) — falling back "
                                f"to `wsl -e tmux` (~130ms/call)")
                            self._mode = "fallback"
        return None


_TMUX = _TmuxChannel()


def _tmux_run(*args):
    """Run one tmux command via the helper, falling back to a `wsl -e tmux` spawn."""
    results = _TMUX.run([list(args)])
    if results is not None and results:
        r = results[0]
        return _TmuxResult(r.get("rc", -1), r.get("out", ""), r.get("err", ""))
    p = subprocess.run(_tmux_cmd(*args), capture_output=True, timeout=15,
                       encoding="utf-8", errors="replace")
    return _TmuxResult(p.returncode, p.stdout or "", p.stderr or "")


def _tmux_literal(text):
    """Escape user text destined for a tmux argv (send-keys -l, set-buffer).

    tmux's lexer strips an unescaped trailing ';' off a word and treats it as a
    command separator, so "ls -la;" arrives as "ls -la" — the semicolon is
    silently swallowed. Escaping it as "\\;" makes tmux deliver it literally.

    Pre-existing: the old `wsl -e tmux` path lost it in exactly the same way,
    which is why this sits above both the helper and the fallback.
    """
    if text.endswith(";"):
        return text[:-1] + "\\;"
    return text


def _tmux_run_many(*cmds):
    """Run several tmux commands in ONE round trip. Returns a result per command."""
    results = _TMUX.run([list(c) for c in cmds])
    if results is not None and len(results) == len(cmds):
        return [_TmuxResult(r.get("rc", -1), r.get("out", ""), r.get("err", ""))
                for r in results]
    return [_tmux_run(*c) for c in cmds]


# File logger — rotating so spark.log can never balloon again (it hit 33MB
# from per-pageload beacons). 2MB cap + one backup is plenty for debugging.
from logging.handlers import RotatingFileHandler
_SPARK_DIR = Path(__file__).resolve().parent
LOG_FILE = _SPARK_DIR / "spark.log"
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                    handlers=[
                        RotatingFileHandler(str(LOG_FILE), maxBytes=2_000_000,
                                            backupCount=1, encoding="utf-8"),
                        logging.StreamHandler(),
                    ])
# Spark is a light personal tool, not a service worth auditing - Flask's
# per-request access log (one line per poll, e.g. GET /api/sessions every few
# seconds) is pure noise here. Keep the app's own action logs (SESSION_SWITCH,
# SEND, BROADCAST, etc.) but drop routine request logging.
logging.getLogger("werkzeug").setLevel(logging.WARNING)
PORT = 5023
HOST = "0.0.0.0"

# Fifteen numbered tabs, each its own color + default Claude model:
#   all opus 5 (2026-08-02: out of fable for the week — every tab defaults to opus).
# Terminal colors are set by the ttyd themes in start.sh (per port); "color"
# here is just the tab accent. "model" is the tab's default for launches.
# Terminals are served same-origin under /term/<tmux> (cloudflare path rules +
# ttyd -b base path). remote_url is relative so it inherits spark's origin and
# its first-party CF Access cookie; local_url hits ttyd directly on localhost.
# Colors are editable in theme.json (single source of truth). "terminal" drives
# the ttyd themes (read by start.sh); "ui" drives the app chrome (read here and
# injected into chat.html). Tabs are neutral — the tab NAMES distinguish sessions;
# the one accent colors the active tab + the ESC/MIC/ENTER buttons (applyTheme).
_THEME_FILE = _SPARK_DIR / "theme.json"

# Gruvbox Light fallback if theme.json is missing or unparseable.
_DEFAULT_UI = {
    "accent": "#af3a03", "bg": "#ebdbb2", "surface": "#fbf1c7",
    "surface_bright": "#f9f5d7", "text": "#3c3836", "text_dim": "#665c54",
    "text_muted": "#7c6f64", "border": "rgba(60,56,54,0.14)",
    "border_strong": "rgba(60,56,54,0.30)", "control_bg": "rgba(235,219,178,0.96)",
}


def _load_theme_ui():
    """Return the 'ui' color dict from theme.json, over the defaults."""
    ui = dict(_DEFAULT_UI)
    try:
        data = json.loads(_THEME_FILE.read_text(encoding="utf-8"))
        ui.update(data.get("ui", {}) or {})
    except (OSError, ValueError):
        pass
    return ui


def _hex_to_rgba(color, alpha):
    """#rrggbb -> 'rgba(r,g,b,a)'. Pass rgba()/unknown through untouched."""
    c = str(color).lstrip("#")
    if len(c) == 6:
        try:
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            return f"rgba({r},{g},{b},{alpha})"
        except ValueError:
            pass
    return color


def _theme_ui_for_template():
    """UI colors plus the accent-derived rgba tints the template needs."""
    ui = _load_theme_ui()
    ui["accent_dim"] = _hex_to_rgba(ui["accent"], 0.15)
    ui["accent_glow"] = _hex_to_rgba(ui["accent"], 0.30)
    ui["theme_dim"] = _hex_to_rgba(ui["accent"], 0.12)
    return ui


# Per-terminal colors (red/green/blue). Each terminal in theme.json's "terminals"
# list has a "color" (the solid identity — tab button + active UI theme) and a
# "background" (the pale tint the terminal itself uses, applied via start.sh).
_DEFAULT_TERMINALS = [
    {"color": "#d23b3b", "background": "#f7dede"},   # red
    {"color": "#3f9d4f", "background": "#dff0e2"},   # green
    {"color": "#3564c0", "background": "#dde8f7"},   # blue
]


def _load_terminals():
    """Return the 'terminals' list from theme.json, over the defaults."""
    terms = [dict(t) for t in _DEFAULT_TERMINALS]
    try:
        data = json.loads(_THEME_FILE.read_text(encoding="utf-8"))
        cfg = data.get("terminals")
        if isinstance(cfg, list) and cfg:
            terms = cfg
    except (OSError, ValueError):
        pass
    return terms


# Seven terminals (2026-08-17, up from five). Adding tabs does NOT add loaded
# iframes: chat.html keeps only the MAX_LOADED (3) most recent live and blanks
# the rest, which is what bounds mobile-renderer memory — see
# gotchas/mobile-renderer-memory.md before raising that cap.
# Reach is fine at seven because ↑ walks back most-recently-used, not by index.
# The tab row stays ONE row at any count: #tab-bar is overflow-x:auto and
# .tab-btn is flex:1, so tabs shrink and ellipsis their names rather than wrap.
# tmux spark8-15 may still be running in the background, just not surfaced here.
# Ports are 7681+n, and cloudflare/config.yml already routes /term/spark1-15.
_TERMINAL_COUNT = 7
_TAB_MODELS = ["opus"] * _TERMINAL_COUNT
_INITIAL_TERMINALS = _load_terminals()
SESSIONS = [
    {"id": str(n), "name": str(n), "tmux": f"spark{n}", "ttyd_port": 7681 + n,
     "local_url": f"http://localhost:{7681 + n}/term/spark{n}",
     "remote_url": f"/term/spark{n}",
     "color": _INITIAL_TERMINALS[(n - 1) % len(_INITIAL_TERMINALS)].get("color", "#af3a03"),
     "bg": _INITIAL_TERMINALS[(n - 1) % len(_INITIAL_TERMINALS)].get("background", "#ffffff"),
     "model": _TAB_MODELS[n - 1]}
    for n in range(1, _TERMINAL_COUNT + 1)
]


def _apply_terminal_colors():
    """Refresh each session's tab color from theme.json 'terminals' (hot-reload)."""
    terms = _load_terminals()
    if not terms:
        return
    for i, s in enumerate(SESSIONS):
        s["color"] = terms[i % len(terms)].get("color", s["color"])
        s["bg"] = terms[i % len(terms)].get("background", s.get("bg", "#ffffff"))

# Model/effort/speed buttons (hamburger quick-switch + the /config page) live
# in models.json (label + literal command, e.g. "/model claude-opus-5" or
# "/fast"). Hot-reloaded on every page load - edit the file, refresh, no
# restart needed. See its "_readme".
_MODELS_FILE = _SPARK_DIR / "models.json"
_DEFAULT_SETTINGS = {
    "model": [
        {"label": "→ Fable 5.1", "command": "/model claude-fable-5-1"},
        {"label": "→ Opus 5", "command": "/model claude-opus-5"},
    ],
    "effort": [],
    "speed": [{"label": "⚡ Toggle Fast Mode", "command": "/fast"}],
}


def _load_settings(key):
    """Return the named button list (model/effort/speed) from models.json."""
    try:
        data = json.loads(_MODELS_FILE.read_text(encoding="utf-8"))
        buttons = data.get(key)
        if isinstance(buttons, list) and buttons:
            return buttons
    except (OSError, ValueError):
        pass
    return _DEFAULT_SETTINGS.get(key, [])


# Terminal names live in terminal_names.txt (format: N=name, blank = number).
# Hot-reloaded on every /api/sessions poll so edits show up on refresh.
_NAMES_FILE = _SPARK_DIR / "terminal_names.txt"


def _load_terminal_names():
    """Return {id: name} for meaningful entries in terminal_names.txt.

    "Meaningful" excludes a name identical to the tab's own number. Found
    2026-10-06: tabs 4 and 7 carried "4=4" and "7=7", which made the server
    treat them as deliberately named, so the manual label beat the workspace
    folder and those two tabs showed "4" and "7" while every other tab showed
    its project. A name that only restates the id carries no information and
    should not outrank the folder.
    """
    names = {}
    try:
        for line in _NAMES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            sid, _, name = line.partition("=")
            sid, name = sid.strip(), name.strip()
            if sid and name and name != sid:
                names[sid] = name
    except OSError:
        pass
    return names


def _apply_terminal_names():
    names = _load_terminal_names()
    for s in SESSIONS:
        s["name"] = names.get(s["id"], s["id"])


def _save_terminal_name(sid, name):
    """Write one terminal's name back to terminal_names.txt."""
    try:
        lines = _NAMES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = [f"{n}=" for n in range(1, _TERMINAL_COUNT + 1)]
    for i, line in enumerate(lines):
        if line.strip().partition("=")[0].strip() == sid:
            lines[i] = f"{sid}={name}"
            break
    else:
        lines.append(f"{sid}={name}")
    _NAMES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


_apply_terminal_names()

# In-memory state
_active_session_id = SESSIONS[0]["id"]
_last_text = None
_text_jobs = {}  # job_id -> "pending" | "sent" | "failed" | "timeout"


def get_session():
    """Get the active session."""
    for s in SESSIONS:
        if s["id"] == _active_session_id:
            return s
    return SESSIONS[0]


# --- Voice commands ---

VOICE_COMMANDS = {
    "enter": "Enter",
    "tab": "Tab",
    "shift tab": "BTab",
    "control c": "C-c",
    "control z": "C-z",
    "control d": "C-d",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "yes": "y Enter",
    "no": "n Enter",
    "one": "1 Enter",
    "two": "2 Enter",
    "three": "3 Enter",
}


def send_to_claude(text, session_id=None):
    """Send keystrokes to a tmux session. Uses explicit session_id if given."""
    global _last_text
    _last_text = text
    if session_id:
        tmux = None
        for s in SESSIONS:
            if s["id"] == session_id:
                tmux = s["tmux"]
                break
        if not tmux:
            tmux = get_session()["tmux"]
    else:
        tmux = get_session()["tmux"]
    cmd = text.strip().lower().rstrip(".")
    key = VOICE_COMMANDS.get(cmd)
    if key:
        result = _tmux_run("send-keys", "-t", tmux, *key.split())
        logging.info(f"KEY cmd='{cmd}' session={tmux} rc={result.returncode}")
    else:
        # Text and Enter in ONE round trip — this was two separate spawns.
        _tmux_run_many(
            ["send-keys", "-t", tmux, "-l", "--", _tmux_literal(text)],
            ["send-keys", "-t", tmux, "Enter"],
        )
        logging.info(f"SEND text='{text[:80]}' session={tmux}")


# --- Main routes ---

# Key-debug flag. Spark is normally opened from the phone's home-screen icon,
# which is a PWA shortcut with a fixed URL — so a ?keys=1 query param never
# survives. Touch this file instead and the next page load comes up in key
# debug mode; delete it to go back to normal.
_KEYDEBUG_FLAG = _SPARK_DIR / ".keydebug"

# job_id -> wall-clock time the button press arrived, so every later stage can
# report "seconds since the press" instead of seconds since some inner step.
# Added 2026-09-30: the old logging bracketed only the MIDDLE of the Listen
# pipeline, which made a 30s round trip look like a 5s one.
_job_start = {}

# --- Warm Piper TTS -------------------------------------------------------
# Local neural TTS. Measured 2026-09-30 on this box:
#   synthesis      0.20s (1 sentence) / 0.37s (3 sentences)
#   gTTS, for comparison: 0.73s / 2.24s over the network
#   voice MODEL LOAD: 1.66s  <-- the whole reason this lives here
# notify.py is spawned fresh per press, so loading the voice there would cost
# 1.66s every time and erase Piper's advantage entirely. app.py is long-lived,
# so it loads the voice ONCE in the background at startup and reuses it.
# If anything here fails, the Listen path silently falls back to the original
# notify.py gTTS route — Piper is an optimization, never a dependency.
_PIPER_MODEL = _SPARK_DIR / "voices" / "en_US-lessac-medium.onnx"
_piper_voice = None
_piper_lock = threading.Lock()


def _piper_warm():
    """Load the voice once, in the background, at startup."""
    global _piper_voice
    if not _PIPER_MODEL.exists():
        logging.info(f"PIPER: no model at {_PIPER_MODEL} — using gTTS path")
        return
    try:
        t = time.time()
        from piper import PiperVoice
        v = PiperVoice.load(str(_PIPER_MODEL))
        with _piper_lock:
            _piper_voice = v
        logging.info(f"PIPER: voice warm in {time.time() - t:.2f}s")
    except Exception as e:
        logging.warning(f"PIPER: load failed ({e}) — using gTTS path")


def _piper_say(text, wav_path):
    """Synthesize to wav with the warm voice. Returns True on success."""
    with _piper_lock:
        v = _piper_voice
    if v is None:
        return False
    try:
        import wave
        t = time.time()
        with wave.open(str(wav_path), "wb") as w:
            v.synthesize_wav(text, w)
        logging.info(f"PIPER: synth {len(text)} chars in {time.time() - t:.2f}s")
        return True
    except Exception as e:
        logging.warning(f"PIPER: synth failed ({e}) — falling back")
        return False


threading.Thread(target=_piper_warm, daemon=True).start()


# ── PWA: installable, chrome-free app ──────────────────────────────────────
# Added 2026-09-30 to get rid of Chrome's URL bar, which was eating ~an inch of
# vertical space. Two routes, both of which MUST live at the site root:
#   /manifest.webmanifest — Flask's static handler serves .webmanifest as
#       octet-stream, which Chrome ignores. Needs the real MIME type.
#   /sw.js — a service worker's SCOPE is its own directory. At /static/sw.js it
#       could only control /static/*; it has to be served from / to control the
#       whole app. This is the classic reason PWA installs silently fail.
@app.route("/manifest.webmanifest")
def manifest():
    resp = send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/offline.html")
def offline_page():
    """Tiny stub shown ONLY when a navigation fails with no network.

    Exists to satisfy Chrome's "does it work offline?" installability check.
    Kept deliberately minimal and self-contained — it must never be mistaken for
    the real UI, and it must never need Spark's CSS or JS to render.
    """
    resp = make_response(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Spark — offline</title></head>"
        "<body style=\"margin:0;display:flex;align-items:center;"
        "justify-content:center;height:100vh;background:#3c3836;color:#ebdbb2;"
        "font-family:system-ui,sans-serif;text-align:center\">"
        "<div><h1 style='margin:0 0 .5rem;font-size:1.3rem'>Spark is offline</h1>"
        "<p style='margin:0;opacity:.75;font-size:.9rem'>No connection to the "
        "terminals. Reconnect and reload.</p></div></body></html>"
    )
    # charset MUST be explicit — without it the em dash in <title> mojibakes.
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/sw.js")
def service_worker():
    """Service worker: network-first, and it caches exactly ONE file.

    Chrome will not offer "Install" unless the app answers a navigation while
    offline — a no-op fetch handler fails that check silently, which is what
    blocked the install on 2026-09-30.

    The critical constraint: it must NOT cache chat.html or style.css. Those
    change constantly, and a cached copy would pin a stale UI on the phone with
    no obvious way to clear it. So the only cached entry is /offline.html, and it
    is served ONLY when a navigation request throws (i.e. no network). Every
    online request, and every non-navigation request, goes straight to the
    network untouched.
    """
    resp = make_response(
        "const CACHE = 'spark-offline-v1';\n"
        "const OFFLINE = '/offline.html';\n"
        "self.addEventListener('install', e => {\n"
        "  e.waitUntil(caches.open(CACHE)\n"
        "    .then(c => c.add(new Request(OFFLINE, {cache: 'reload'})))\n"
        "    .then(() => self.skipWaiting()));\n"
        "});\n"
        "self.addEventListener('activate', e => {\n"
        "  e.waitUntil(caches.keys()\n"
        "    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))\n"
        "    .then(() => self.clients.claim()));\n"
        "});\n"
        "self.addEventListener('fetch', e => {\n"
        "  // Navigations only. Network first; the cached stub is a last resort.\n"
        "  if (e.request.mode !== 'navigate') return;  // everything else: untouched\n"
        "  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));\n"
        "});\n"
    )
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/")
def home():
    _apply_terminal_names()
    _apply_terminal_colors()
    active = get_session()
    resp = make_response(render_template("chat.html",
        session=active, sessions=SESSIONS, theme_ui=_theme_ui_for_template(),
        model_buttons=_load_settings("model"),
        key_debug=_KEYDEBUG_FLAG.exists()))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/config")
def config_page():
    """Model/effort/speed settings page, reached from the hamburger's ⚙ Config
    button. Buttons come from models.json (hot-reloaded); each one sends its
    command to either just the active terminal or every terminal at once."""
    resp = make_response(render_template("config.html",
        session=get_session(), sessions=SESSIONS, theme_ui=_theme_ui_for_template(),
        model_buttons=_load_settings("model"),
        effort_buttons=_load_settings("effort"),
        speed_buttons=_load_settings("speed")))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _send_and_report(sid, command, wait=0.4):
    """Send a literal command to a session, then read back Claude Code's own
    reply line (the "⎿ ..." line it prints under a command like /fast or
    /effort) so the UI can show the real resulting state, not just 'sent'.

    Some commands need a follow-up keypress before they actually take effect,
    and left unanswered that leaves the session silently stuck (seen live,
    2026-09-29 - looked like "the API going in and out" from the phone, but
    both terminals were just waiting on a keypress that never came):
      - /effort asks "re-read full history - Yes/No" -> answer "1" (yes).
      - /fast shows "Fast mode OFF ... Tab to toggle - Enter to confirm" ->
        Tab actually flips the shown value, then Enter confirms it. Without
        this, "Toggle Fast Mode" never toggled anything - it just opened the
        dialog and stopped.
    The button press already means "go ahead", so auto-answer both instead
    of hanging."""
    send_to_claude(command, session_id=sid)
    time.sleep(wait)
    tmux = next((s["tmux"] for s in SESSIONS if s["id"] == sid), None)
    if not tmux:
        return None
    result = _tmux_run("capture-pane", "-t", tmux, "-p")
    text = result.stdout or ""
    if "1. Yes" in text and "2. No" in text:
        _tmux_run("send-keys", "-t", tmux, "1", "Enter")
        time.sleep(wait)
        result = _tmux_run("capture-pane", "-t", tmux, "-p")
        text = result.stdout or ""
    elif "Tab to toggle" in text:
        _tmux_run("send-keys", "-t", tmux, "Tab")
        time.sleep(0.15)
        _tmux_run("send-keys", "-t", tmux, "Enter")
        time.sleep(wait)
        result = _tmux_run("capture-pane", "-t", tmux, "-p")
        text = result.stdout or ""
    for line in reversed(text.splitlines()):
        if "⎿" in line:
            return line.split("⎿", 1)[1].strip()
    return None


@app.route("/api/broadcast", methods=["POST"])
def api_broadcast():
    """Send a literal command (from the /config page) to one or all terminals,
    reporting back each terminal's actual resulting state where Claude Code
    prints one (e.g. "Fast mode ON")."""
    data = request.get_json()
    command = (data.get("command") or "").strip()
    scope = data.get("scope", "active")
    if not command:
        return jsonify({"error": "No command"}), 400
    if scope == "all":
        results = [{"name": s["name"], "reply": _send_and_report(s["id"], command)}
                   for s in SESSIONS]
        logging.info(f"BROADCAST '{command}' -> all {len(SESSIONS)} terminals: {results}")
        return jsonify({"ok": True, "scope": scope, "results": results})
    else:
        active = get_session()
        reply = _send_and_report(active["id"], command)
        logging.info(f"BROADCAST '{command}' -> active ({active['tmux']}): {reply}")
        return jsonify({"ok": True, "scope": scope, "reply": reply})


@app.route("/test")
def test_page():
    """Dead-simple baseline page — no terminals, no iframes, no external CSS.
    If this renders on the phone, the browser/tunnel/Access/Spark are all fine
    and the problem is isolated to the terminal page."""
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Spark Test</title>
<style>html,body{margin:0;padding:0;height:100%;}
.band{height:20vh;display:flex;align-items:center;justify-content:center;color:#fff;font:700 26px sans-serif;}</style>
</head>
<body>
<div class="band" style="background:#e11d48;">TOP (red)</div>
<div class="band" style="background:#ea580c;">2 (orange)</div>
<div class="band" style="background:#059669;">MIDDLE (green)</div>
<div class="band" style="background:#2563eb;">4 (blue)</div>
<div class="band" style="background:#7c3aed;">BOTTOM (purple)</div>
<script>
fetch('/api/log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:'[CLIENT] TEST BANDS rendered win='+window.innerWidth+'x'+window.innerHeight+' scrollY='+window.scrollY+' docH='+document.documentElement.scrollHeight})}).catch(function(){});
</script>
</body></html>"""
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# --- Tmux helpers ---

def _pane_info():
    """Return {tmux_session: {"cmd": ..., "path": ...}} for all sessions."""
    try:
        result = _tmux_run(
            "list-panes", "-a", "-F",
            "#{session_name}\t#{pane_current_command}\t#{pane_current_path}")
        if result.returncode != 0:
            return {}
        out = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = {"cmd": parts[1], "path": parts[2]}
        return out
    except Exception:
        return {}


def _pane_commands():
    return {k: v["cmd"] for k, v in _pane_info().items()}


_SHELLS = {"bash", "sh", "zsh", "fish", "dash"}


# --- API routes ---

@app.route("/api/sessions")
def api_sessions():
    """Session list for the tab bar — now including WHERE each pane is.

    _pane_info() has always returned every pane's path in a single tmux call;
    _pane_commands() then dropped it on the floor. The folder was free all
    along. Carrying it through is what lets the tabs say "camino" because the
    pane is IN camino, rather than because someone typed that label once and
    the pane has since wandered somewhere else.

    No new state file: a terminal's folder is its own cwd, and `cd` is how you
    point a slot at a project. tmux is the source of truth.
    """
    _apply_terminal_names()
    _apply_terminal_colors()
    info = _pane_info()
    names = _load_terminal_names()
    root = FILES_ROOT.resolve()
    out = []
    for s in SESSIONS:
        d = dict(s)
        pane = info.get(s["tmux"]) or {}
        cmd = pane.get("cmd")
        d["running"] = cmd
        d["alive"] = bool(cmd) and cmd not in _SHELLS

        cwd = ""
        try:
            raw = _wsl_to_win(pane.get("path") or "")
            if raw:
                pth = Path(raw).resolve()
                if pth == root or pth.is_relative_to(root):
                    cwd = "" if pth == root else \
                        str(pth.relative_to(root)).replace("\\", "/")
        except Exception:
            pass
        d["cwd"] = cwd

        # The tab's label comes from its WORKSPACE, not its pane cwd. The
        # workspace is what Patrick assigns and what the panes follow; a pane's
        # cwd is wherever its shell happens to sit (all seven sit at /dev root).
        # Last segment only: "revel/atrium" is the folder, "atrium" is the name
        # he calls it.
        ws_folder = _workspace(s["id"])["folder"]
        folder = ws_folder or cwd
        d["folder"] = folder
        d["project"] = (folder.rstrip("/").split("/")[-1] if folder else "dev")
        # Manual names win — "election" is not a folder name, and Patrick
        # should keep the right to call a slot whatever he wants.
        d["named"] = bool(names.get(s["id"]))
        out.append(d)
    return jsonify({"sessions": out, "active": _active_session_id})


# Verified against the live Models API 2026-07-25 — claude-opus-5 is real.
CLAUDE_MODELS = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
CLAUDE_EFFORTS = ["low", "medium", "high"]

LAUNCH_COMMANDS = {
    "claude": "claude --model {model} --effort {effort}",
    "gemini": "gemini",
    "chatgpt": "codex",
    "terminal": "clear",
}


@app.route("/api/session/launch", methods=["POST"])
def launch_session():
    """Kill whatever runs in a session's pane and launch a fresh CLI."""
    data = request.get_json()
    sid = data.get("id", "")
    cli = data.get("cli", "")
    cmd = LAUNCH_COMMANDS.get(cli)
    if not cmd:
        return jsonify({"error": "Unknown cli"}), 400
    if cli == "claude":
        # Default model = the tab's own model (all tabs default to opus);
        # an explicit "model" in the request overrides it. Effort is only passed
        # when explicitly requested — otherwise claude uses the settings.json
        # default (high), matching how sessions launch outside Spark.
        tab_default = next((s["model"] for s in SESSIONS if s["id"] == sid), "opus")
        model_key = data.get("model") or tab_default
        model_id = CLAUDE_MODELS.get(model_key, CLAUDE_MODELS["opus"])
        effort = data.get("effort")
        cmd = f"claude --model {model_id}"
        if effort in CLAUDE_EFFORTS:
            cmd += f" --effort {effort}"
    for s in SESSIONS:
        if s["id"] == sid:
            tmux = s["tmux"]
            _tmux_run("respawn-pane", "-k", "-t", tmux)
            time.sleep(0.5)  # let the fresh shell come up before typing into it
            # "Open a terminal HERE" — the caller may pass the folder it is
            # looking at. Guarded by _files_safe, so a bad path falls back to
            # /dev rather than cd-ing somewhere off the workspace.
            work_dir = "/mnt/c/dev"
            want = _files_safe(data.get("path") or "")
            if want is not None and want.exists():
                if want.is_file():
                    want = want.parent
                sub = str(want.relative_to(FILES_ROOT.resolve())).replace("\\", "/")
                if sub and sub != ".":
                    work_dir = "/mnt/c/dev/" + sub
            _tmux_run("send-keys", "-t", tmux,
                      f"cd {work_dir} && {cmd}", "Enter")
            logging.info(f"LAUNCH {cli} in {tmux} ({cmd})")
            return jsonify({"ok": True, "cmd": cmd})
    return jsonify({"error": "Unknown session"}), 400


@app.route("/api/session", methods=["POST"])
def set_session():
    global _active_session_id
    data = request.get_json()
    sid = data.get("id", "")
    for s in SESSIONS:
        if s["id"] == sid:
            _active_session_id = sid
            logging.info(f"SESSION_SWITCH -> {s['name']} (tmux={s['tmux']})")
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


@app.route("/api/session/rename", methods=["POST"])
def rename_session():
    data = request.get_json()
    sid = data.get("id", "")
    name = (data.get("name") or "").strip()
    if not name:
        # Blank is a real instruction: drop the custom label and let the tab
        # go back to being named after its workspace folder.
        for s in SESSIONS:
            if s["id"] == sid:
                _save_terminal_name(sid, "")
                _apply_terminal_names()
                logging.info(f"SESSION_RENAME {sid} -> (cleared)")
                return jsonify({"ok": True, "cleared": True})
        return jsonify({"error": "Unknown session"}), 400
    for s in SESSIONS:
        if s["id"] == sid:
            s["name"] = name
            _save_terminal_name(sid, name)
            logging.info(f"SESSION_RENAME {sid} -> {name}")
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


ALLOWED_KEYS = {
    "Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right",
    "Space", "PageUp", "PageDown", "C-c", "C-z", "C-d", "C-u", "BSpace",
    # C-l clears the visible screen and leaves the session alone - the R2
    # button (2026-09-29). Without it here the button would POST and get back
    # "Key not allowed", which looks exactly like a dead button.
    "C-l",
}


def _resolve_session(data=None):
    """Resolve tmux session name from request data or fall back to active."""
    sid = (data or {}).get("session", "")
    if sid:
        for s in SESSIONS:
            if s["id"] == sid:
                return s["tmux"]
    return get_session()["tmux"]


# --- File rail -------------------------------------------------------------
# Lists the active terminal's working directory so the UI can offer tap-to-insert
# paths. Dictating "templates/chat.html" by voice is the single worst friction in
# Spark; tapping it is instant.
#
# SECURITY: jailed to FILES_ROOT, and that is not optional. Spark is reachable
# from the public internet through the tunnel. Until now its API could only
# inject keystrokes; a directory lister is the first endpoint that can READ the
# disk, so an unconstrained one would hand the whole filesystem to anyone who got
# past the token. Every path is resolved and then re-checked to be inside the
# root, which also kills "..", symlinks, and absolute-path escapes.
FILES_ROOT = Path("/mnt/c/dev") if not _IS_WINDOWS else Path("C:/dev")
_FILES_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv",
               ".pytest_cache", ".mypy_cache", ".idea", ".vscode"}


def _wsl_to_win(raw):
    """/mnt/c/dev/spark -> C:/dev/spark.

    tmux lives in WSL and reports WSL paths; this Flask app is a Windows
    process, where Path("/mnt/c/dev") resolves to C:\\mnt\\c\\dev and is
    relative to nothing. Found 2026-10-02: every pane's cwd was being discarded
    by that mismatch, which is also why the file rail always opened at /dev
    root no matter where the terminal actually was.
    """
    t = (raw or "").strip().replace("\\", "/")
    if len(t) > 6 and t[:5].lower() == "/mnt/" and t[6] == "/":
        return t[5].upper() + ":" + t[6:]
    return t


def _files_safe(raw):
    """Resolve `raw` under FILES_ROOT, or return None if it escapes."""
    try:
        root = FILES_ROOT.resolve()
        target = (root / (raw or "").lstrip("/\\")).resolve()
        # is_relative_to is the whole guard — do not replace with startswith on
        # strings, which "/mnt/c/devil" would pass.
        return target if target == root or target.is_relative_to(root) else None
    except Exception:
        return None


def _pane_cwd_rel(tmux):
    """The terminal's cwd, as a path relative to FILES_ROOT ('' if outside)."""
    r = _tmux_run("display-message", "-p", "-t", tmux, "#{pane_current_path}")
    cwd = _wsl_to_win((r.stdout or "").strip())
    if not cwd:
        return ""
    try:
        p = Path(cwd).resolve()
        root = FILES_ROOT.resolve()
        if p == root or p.is_relative_to(root):
            return str(p.relative_to(root)).replace("\\", "/").strip(".")
    except Exception:
        pass
    return ""


# Code folder or information folder? Spark's /dev holds both — manna and spark
# are code, while camino, haven, tome and nota are stacks of documents with no
# source in them. They want different treatment: "what changed" means a git
# diff in a code folder and "which files are new" in an information folder.
#
# Classified by a SHALLOW scan — this runs on every listing, so it reads one
# directory level and stops. Two levels deep would mean walking node_modules.
_CODE_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sh",
             ".bat", ".sql", ".ipynb", ".go", ".rs", ".java", ".ps1"}
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_DOC_EXT = {".pdf", ".docx", ".md", ".txt", ".csv", ".xlsx", ".heic",
            ".eml", ".pptx"} | _IMG_EXT


def _folder_kind(target):
    """'code' | 'info' | 'charts' — what KIND of folder this is.

    Three kinds because Patrick's /dev has three. manna and spark are code.
    camino and tome are stacks of documents. vigor is 71 PNGs of charts and 17
    PDFs sitting next to 33 scripts nobody wants to look at — counting source
    files would call it code, which is true and useless. What he wants from
    vigor is the pictures.

    So: images are weighed on their own. If they dominate what is NOT source,
    the folder is a chart folder regardless of how many .py files are lying
    around generating them.
    """
    code = doc = img = 0
    try:
        for e in target.iterdir():
            if e.name.startswith(".") or e.name in _FILES_SKIP:
                continue
            if e.is_dir():
                continue
            ext = e.suffix.lower()
            if ext in _IMG_EXT:
                img += 1
                doc += 1
            elif ext in _CODE_EXT:
                code += 1
            elif ext in _DOC_EXT:
                doc += 1
    except (OSError, PermissionError):
        return "code"
    # Charts first: this is the one kind that survives a pile of source files,
    # because the source is the chart GENERATOR, not the point.
    if img >= 5 and doc and img / doc >= 0.4:
        return "charts"
    # A git repo is code even when the top level is all README — the source is
    # a directory down. Ties go to code: showing a diff pane for a document
    # folder is harmless, hiding it for a code folder is not.
    if (target / ".git").exists():
        return "code"
    return "info" if doc > code else "code"


@app.route("/api/files")
def api_files():
    """Directory listing for the rail. ?path= is relative to FILES_ROOT;
    omit it to follow the active terminal's cwd."""
    raw = request.args.get("path")
    if raw is None:
        raw = _pane_cwd_rel(_resolve_session({"session": request.args.get("session")}))
    target = _files_safe(raw)
    if target is None or not target.exists():
        target = FILES_ROOT.resolve()
    if target.is_file():
        target = target.parent

    dirs, files = [], []
    try:
        for e in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if e.name.startswith(".") or e.name in _FILES_SKIP:
                continue
            (dirs if e.is_dir() else files).append(e.name)
    except PermissionError:
        pass

    root = FILES_ROOT.resolve()
    rel = "" if target == root else str(target.relative_to(root)).replace("\\", "/")
    # parent: None at the root (so the UI hides the ".." row), "" one level down
    # (meaning "go to the root"), otherwise the parent's relative path.
    if not rel:
        parent = None
    else:
        up = str(Path(rel).parent).replace("\\", "/")
        parent = "" if up == "." else up
    return jsonify({
        "root": str(root).replace("\\", "/"),
        "path": rel,
        "parent": parent,
        "kind": _folder_kind(target),
        "dirs": dirs[:400],
        "files": files[:400],
    })


# --- Workspaces ------------------------------------------------------------
# A terminal is not just a pane any more, it is a WORKSPACE: a folder, the file
# you had open, and which view the middle pane was showing. Switching tabs
# restores all three, so tab 5 is "vigor, looking at the charts" and tab 2 is
# "camino, reading that PDF" — and the file rail is that folder's explorer, not
# a browser of all of /dev.
#
# Server-side rather than localStorage on purpose: the phone and the desktop
# are the same seven workspaces, and Patrick would rather have the state in a
# file he can read than in a browser he cannot.
_WS_FILE = _SPARK_DIR / "_workspaces.json"


def _load_workspaces():
    try:
        d = json.loads(_WS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_workspaces(d):
    try:
        _WS_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError:
        pass


def _workspace(sid):
    """One session's workspace, falling back to its pane cwd then to /dev."""
    ws = _load_workspaces().get(str(sid)) or {}
    folder = ws.get("folder")
    if folder is None:
        # Never been set: start where the pane actually is.
        tmux = next((x["tmux"] for x in SESSIONS if x["id"] == str(sid)), None)
        folder = _pane_cwd_rel(tmux) if tmux else ""
    return {
        "folder": folder or "",
        "file": ws.get("file") or "",
        "view": ws.get("view") or "",
    }


@app.route("/api/workspace", methods=["GET", "POST"])
def api_workspace():
    """GET ?session=N -> that workspace. POST {session, folder?, file?, view?}.

    Partial writes: only the keys present are changed, so saving the open file
    does not clobber the folder.
    """
    if request.method == "GET":
        sid = request.args.get("session") or _active_session_id
        return jsonify(_workspace(sid))

    data = request.get_json() or {}
    sid = str(data.get("session") or _active_session_id)
    all_ws = _load_workspaces()
    ws = all_ws.get(sid) or {}
    for key in ("folder", "file", "view"):
        if key in data:
            val = (data.get(key) or "").strip().strip("/")
            if key in ("folder", "file") and val:
                # Everything stored is /dev-relative and guarded on the way in.
                if _files_safe(val) is None:
                    return jsonify({"error": "outside /dev"}), 400
            ws[key] = val
    all_ws[sid] = ws
    _save_workspaces(all_ws)
    return jsonify({"ok": True, **_workspace(sid)})


@app.route("/api/projects")
def api_projects():
    """Top-level /dev folders, with each one's kind — the project picker."""
    root = FILES_ROOT.resolve()
    out = []
    try:
        for e in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if e.is_dir() and not e.name.startswith(".") and e.name not in _FILES_SKIP:
                out.append({"name": e.name, "kind": _folder_kind(e)})
    except (OSError, PermissionError):
        pass
    return jsonify({"projects": out})


@app.route("/api/project/new", methods=["POST"])
def api_project_new():
    """Create a project: folder, git repo, .gitignore, README, first commit.

    The first five minutes of a fresh build are mkdir, git init, a .gitignore,
    a README and a commit — all of it typing, none of it thinking, and all of
    it on screen if someone is watching. This does the lot in one call and
    hands back a folder that is already a repo with history.

    Deliberately NOT a scaffold: no framework, no src/, no chosen stack. What
    the project is gets decided after the problem is read, not before.
    """
    data = request.get_json() or {}
    raw = (data.get("name") or "").strip().strip("/")
    # One path segment, conservative character set — this creates directories.
    if not raw or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,48}", raw):
        return jsonify({"error": "name must be letters, numbers, - or _"}), 400
    name = raw.replace(" ", "_").lower()

    target = _files_safe(name)
    if target is None:
        return jsonify({"error": "outside /dev"}), 400
    if target.exists():
        return jsonify({"error": f"{name} already exists"}), 400

    try:
        target.mkdir(parents=True)
    except OSError as e:
        return jsonify({"error": str(e)[:120]}), 500

    (target / ".gitignore").write_text("\n".join([
        ".env", "*.log", "*.pyc", "__pycache__/", ".venv/", "venv/",
        "node_modules/", "token.json", "credentials.json", "*.sqlite",
        ".DS_Store", "",
    ]), encoding="utf-8")
    (target / "README.md").write_text(
        f"# {name}\n\nCreated {datetime.now().strftime('%Y-%m-%d')}.\n\n"
        "## What this is\n\n_One sentence, written before the first line of code._\n",
        encoding="utf-8")

    rc, _ = _git(target, "init")
    if rc != 0:
        return jsonify({"error": "git init failed", "path": name}), 500
    # Without this, Windows git normalises the LF files written above to CRLF
    # on checkout and the brand-new repo reports modified files one second
    # after its first commit. A fresh project should be clean.
    _git(target, "config", "core.autocrlf", "false")
    _git(target, "branch", "-M", "main")
    _git(target, "add", "-A")
    # An empty-ish first commit on purpose: it is the floor to fall back to,
    # and everything after it is readable as a diff against "nothing".
    _git(target, "-c", "user.useConfigOnly=false",
         "commit", "-m", f"{name}: empty project, git from the first minute")

    # Point a terminal at it if the caller asked.
    sid = str(data.get("session") or "")
    if sid:
        all_ws = _load_workspaces()
        all_ws[sid] = {"folder": name, "file": "", "view": ""}
        _save_workspaces(all_ws)

    rc, log = _git(target, "--no-pager", "log", "--oneline", "-1")
    return jsonify({"ok": True, "path": name, "first_commit": log.strip()})


@app.route("/dashboard")
def dashboard_page():
    resp = make_response(render_template("dashboard.html",
                                         theme_ui=_theme_ui_for_template()))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/dashboard")
def api_dashboard():
    """Everything the second screen shows, in one poll."""
    s = get_session()
    lines = int(request.args.get("lines", 24))
    lines = max(5, min(lines, 80))
    text = _capture_scrollback(lines=lines, session_id=s["id"])

    # Git summary for the terminal's WORKSPACE, not its pane cwd.
    #
    # Found 2026-10-07: the dashboard's git panel was always blank. It asked
    # the pane where it was, and every pane runs claude from /dev root — which
    # is not a repo — so the panel reported nothing for every terminal. The
    # workspace is the folder Patrick ASSIGNED to the tab, which is the thing
    # he means by "what am I working on".
    git = {"repo": None, "changed": 0, "branch": None}
    try:
        folder = _workspace(s["id"])["folder"] or _pane_cwd_rel(s["tmux"])
        target = _files_safe(folder) or FILES_ROOT.resolve()
        repo = _git_root(target if target.is_dir() else target.parent)
        if repo is not None:
            rc, out = _git(repo, "status", "--porcelain=v1")
            rc2, br = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            root = FILES_ROOT.resolve()
            git = {
                "repo": "" if repo == root else str(repo.relative_to(root)).replace("\\", "/"),
                "changed": len([l for l in out.splitlines() if l.strip()]),
                "branch": (br or "").strip() or None,
            }
    except Exception as e:
        logging.warning(f"DASHBOARD git: {e}")

    # What has just been written in this workspace. The second screen's job is
    # to answer "what is it doing" from ten feet away, and a list of files that
    # changed in the last few minutes answers that better than scrollback does.
    activity = []
    try:
        folder = _workspace(s["id"])["folder"]
        base = _files_safe(folder) if folder else None
        if base is not None and base.is_dir():
            found = []
            for f in list(base.glob("*"))[:400]:
                if f.is_file() and not f.name.startswith("."):
                    found.append(f)
            for d in list(base.glob("*/"))[:30]:
                if d.is_dir() and not d.name.startswith((".", "_")):
                    for f in list(d.glob("*"))[:200]:
                        if f.is_file() and not f.name.startswith("."):
                            found.append(f)
            now = time.time()
            found.sort(key=lambda f: -f.stat().st_mtime)
            root = FILES_ROOT.resolve()
            for f in found[:8]:
                age = now - f.stat().st_mtime
                activity.append({
                    "name": f.name,
                    "path": str(f.relative_to(root)).replace("\\", "/"),
                    "mins": int(age // 60),
                })
    except Exception as e:
        logging.warning(f"DASHBOARD activity: {e}")

    return jsonify({
        "session": {"id": s["id"], "name": s["name"], "color": s.get("color"),
                    "tmux": s["tmux"], "model": s.get("model")},
        "screen": text,
        "git": git,
        "workspace": _workspace(s["id"])["folder"],
        "activity": activity,
        "sessions": [{"id": x["id"], "name": x["name"], "color": x.get("color")}
                     for x in SESSIONS],
    })


@app.route("/api/key", methods=["POST"])
def key():
    data = request.get_json()
    k = (data.get("key") or "").strip()
    if k not in ALLOWED_KEYS:
        return jsonify({"error": "Key not allowed"}), 400
    tmux = _resolve_session(data)
    result = _tmux_run("send-keys", "-t", tmux, k)
    if result.returncode != 0:
        return jsonify({"error": f"tmux failed (rc={result.returncode})"}), 500
    return jsonify({"ok": True, "session": tmux})


@app.route("/api/type", methods=["POST"])
def type_char():
    """Send raw characters to tmux without Enter — for keyboard typing."""
    data = request.get_json()
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "No text"}), 400
    tmux = _resolve_session(data)
    _tmux_run("send-keys", "-t", tmux, "-l", "--", _tmux_literal(text))
    return jsonify({"ok": True})


@app.route("/api/scroll", methods=["POST"])
def scroll():
    data = request.get_json()
    direction = data.get("direction", "up")
    tmux = _resolve_session(data)

    # Two scroll worlds depending on the pane's screen buffer:
    #  - ALTERNATE screen on (a full-screen TUI like Claude Code owns the pane):
    #    the app has its OWN scrollback and tmux copy-mode would only surface the
    #    pre-launch banner. Send the real PageUp/PageDown keys so the app scrolls.
    #  - NORMAL screen (Claude rendering inline, or a plain shell): the scrollback
    #    lives in tmux, so a PageUp keystroke goes nowhere — drive tmux copy-mode.
    # Detect per-call because a tab can flip between the two (e.g. Bravo was in the
    # normal buffer while the others were alt-screen, which knocked out its PageUp).
    alt = _tmux_run("display-message", "-p", "-t", tmux,
                    "#{alternate_on}").stdout.strip()

    if alt == "1":
        if direction == "up":
            _tmux_run("send-keys", "-t", tmux, "PageUp")
        else:
            # down = a single page, symmetric with PageUp. Reaching the bottom
            # returns to the live input; typing also auto-snaps there.
            _tmux_run("send-keys", "-t", tmux, "PageDown")
    else:
        if direction == "up":
            # -e = auto-exit copy-mode when scrolled back to the bottom.
            # Both commands are ours (no user text), so batching them into one
            # round trip is safe — nothing here can be mistaken for a separator.
            _tmux_run_many(
                ["copy-mode", "-e", "-t", tmux],
                ["send-keys", "-X", "-t", tmux, "page-up"],
            )
        else:
            # page-down inside copy-mode; a no-op (and stays live) if not scrolled
            _tmux_run("send-keys", "-X", "-t", tmux, "page-down")
    return jsonify({"ok": True})


@app.route("/api/screenshot", methods=["POST"])
def screenshot():
    data = request.get_json() or {}
    tmux = _resolve_session(data)
    result = _tmux_run("capture-pane", "-t", tmux, "-p")
    text = (result.stdout or "").rstrip()
    return jsonify({"ok": True, "text": text})


@app.route("/api/screenshot/save", methods=["POST"])
def screenshot_save():
    """Capture visible pane and save to C:/dev/."""
    data = request.get_json() or {}
    tmux = _resolve_session(data)
    result = _tmux_run("capture-pane", "-t", tmux, "-p")
    text = (result.stdout or "").rstrip()
    if not text:
        return jsonify({"ok": False, "error": "Empty capture"}), 400
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{ts}.txt"

    def _write():
        for base in [Path("C:/dev"), Path("/mnt/c/dev")]:
            try:
                (base / filename).write_text(text, encoding="utf-8")
                logging.info(f"SCREENSHOT_SAVE: {base / filename}")
                return
            except Exception:
                continue
        logging.error("SCREENSHOT_SAVE: all write paths failed")

    threading.Thread(target=_write, daemon=True).start()
    return jsonify({"ok": True})


def _retrieve_last_turn(session_id=None):
    """The shared engine behind Listen and Text-me. Identifies which Claude Code
    conversation the tab is showing (via listen_retrieve.py's fingerprint) and
    pulls its last real turn from the transcript file — clean, full, right tab.
    Returns the parsed dict, or None if the engine itself failed (not just 'empty')."""
    tmux = _resolve_session({"session": session_id} if session_id else {})
    try:
        result = subprocess.run(
            _PY_PREFIX + ["/mnt/c/dev/spark/listen_retrieve.py", tmux],
            capture_output=True, timeout=20, encoding="utf-8", errors="replace",
        )
        out = (result.stdout or "").strip()
        if not out:
            logging.error(f"RETRIEVE empty (stderr={(result.stderr or '').strip()[:200]})")
            return None
        return json.loads(out.splitlines()[-1])
    except Exception as e:
        logging.error(f"RETRIEVE: {e}")
        return None


def _turn_to_text(turn):
    """Format a retrieved turn as clean input for the notify.py formatters."""
    return (f"[PATRICK ASKED]\n{(turn.get('user') or '').strip()}\n\n"
            f"[CLAUDE REPLIED]\n{(turn.get('assistant') or '').strip()}\n")


def _capture_scrollback(lines=200, session_id=None):
    """Capture last N lines of scrollback from given (or active) tmux session."""
    tmux = _resolve_session({"session": session_id} if session_id else {})
    result = _tmux_run("capture-pane", "-t", tmux, "-p", "-S", f"-{lines}")
    return (result.stdout or "").strip()


def _write_scrollback(text):
    """Write scrollback to temp file. Returns (local_path, windows_path)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Try Windows path first, fall back to WSL path
    for base in [Path("C:/dev/spark"), Path("/mnt/c/dev/spark")]:
        try:
            tmp = base / f"_scrollback_{ts}.txt"
            tmp.write_text(text, encoding="utf-8")
            win_path = f"C:/dev/spark/_scrollback_{ts}.txt"
            return tmp, win_path
        except Exception:
            continue
    raise RuntimeError("Cannot write scrollback temp file")


@app.route("/api/text-me", methods=["POST"])
def text_me():
    """Pull Claude's last reply from the transcript, clean for SMS, send via buzz."""
    data = request.get_json() or {}
    sid = data.get("session")
    turn = _retrieve_last_turn(sid)
    if turn is None:
        text = _capture_scrollback(lines=50, session_id=sid)
        if not text:
            return jsonify({"ok": False, "error": "Nothing to capture"}), 400
    elif turn.get("ok") and turn.get("assistant"):
        text = _turn_to_text(turn)
        logging.info(f"TEXT_ME via transcript {turn.get('file')}")
    else:
        return jsonify({"ok": False, "error": "Nothing to read yet"}), 400
    tmp, win_tmp = _write_scrollback(text)

    job_id = str(uuid.uuid4())[:8]
    _text_jobs[job_id] = "pending"

    def _do():
        try:
            result = subprocess.run(
                [_WIN_PYTHON, "C:/dev/spark/notify.py", "text", win_tmp],
                capture_output=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                logging.info(f"TEXT_ME OK: {result.stdout.strip()}")
                _text_jobs[job_id] = "sent"
            else:
                logging.error(f"TEXT_ME_ERR: {result.stderr.strip()}")
                _text_jobs[job_id] = "failed"
        except subprocess.TimeoutExpired:
            logging.error("TEXT_ME: timed out after 30s")
            _text_jobs[job_id] = "timeout"
        except Exception as e:
            logging.error(f"TEXT_ME: {e}")
            _text_jobs[job_id] = "failed"
        finally:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "job": job_id})


@app.route("/api/play-me", methods=["POST"])
def play_me():
    """Capture scrollback, summarize as Alan Watts, TTS + Telegram."""
    data = request.get_json() or {}
    sid = data.get("session")
    text = _capture_scrollback(lines=50, session_id=sid)
    if not text:
        return jsonify({"ok": False, "error": "Nothing to capture"}), 400
    tmp, win_tmp = _write_scrollback(text)

    job_id = str(uuid.uuid4())[:8]
    _text_jobs[job_id] = "pending"

    def _do():
        try:
            result = subprocess.run(
                [_WIN_PYTHON, "C:/dev/spark/notify.py", "play", win_tmp],
                capture_output=True, timeout=90,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                logging.info(f"PLAY_ME OK: {result.stdout.strip()}")
                _text_jobs[job_id] = "sent"
            else:
                logging.error(f"PLAY_ME_ERR: {result.stderr.strip()}")
                _text_jobs[job_id] = "failed"
        except subprocess.TimeoutExpired:
            logging.error("PLAY_ME: timed out after 60s")
            _text_jobs[job_id] = "timeout"
        except Exception as e:
            logging.error(f"PLAY_ME: {e}")
            _text_jobs[job_id] = "failed"
        finally:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "job": job_id})


_audio_files = {}  # job_id -> mp3 path


# --- Listen spend guard ---------------------------------------------------
# Listen is the one path that bills: notify.py's `listen`/`vsummary` modes go
# through Patrick's own Anthropic key, not mente's subscription. The controller
# auto-repeats while a button is held, and on 2026-08-17 one hold fired 356
# billed calls (~360k input tokens) in a day — against 43 on every other day
# combined.
#
# The guard lives here, not in the browser, because auto-repeat fires ~10-26
# times a SECOND: any client-side flag loses that race. Two rules:
#   1. While a job is running for this (session, mode), re-requests return it.
#   2. For a short cooldown after it starts, re-requests return it too — that
#      catches the tail of a hold that lands just as a job completes.
# Coalesced requests return ok + the existing job id, so a double-tap plays the
# audio it already asked for instead of surfacing an error.
#
# NOT "a second press cancels the first": with auto-repeat that turns a held
# button into hundreds of start/kill cycles, which is worse than the bug.
_LISTEN_COOLDOWN = 5.0
_listen_guard_lock = threading.Lock()
_listen_recent = {}  # (session, mode) -> {"job": id, "started": ts}


@app.route("/api/listen", methods=["POST"])
def listen_me():
    """Capture scrollback, summarize ONLY the latest response (API path), TTS
    to mp3 — played back in-browser. Captures more lines than the SMS/Play
    paths so Patrick's last input is reliably in the window to slice from."""
    data = request.get_json() or {}
    sid = data.get("session")
    # mode: "listen" = full reply read aloud (Y). "vsummary" = 1-3 sentence spoken
    # summary (X) — same content as the Text button, but voiced instead of texted.
    mode = data.get("mode", "listen")
    if mode not in ("listen", "vsummary"):
        mode = "listen"

    _t0 = time.time()   # TIMING: grep spark.log for "LISTEN_T"

    # Spend guard — must run BEFORE _retrieve_last_turn(), which spawns its own
    # WSL process, and long before the billed call in notify.py.
    guard_key = (sid or "_active", mode)
    with _listen_guard_lock:
        prev = _listen_recent.get(guard_key)
        if prev:
            age = time.time() - prev["started"]
            running = _text_jobs.get(prev["job"]) == "pending"
            if running or age < _LISTEN_COOLDOWN:
                logging.info(
                    f"LISTEN coalesced ({mode}) -> job={prev['job']} "
                    f"age={age:.1f}s running={running} — no new API call")
                return jsonify({"ok": True, "job": prev["job"],
                                "coalesced": True})

    turn = _retrieve_last_turn(sid)
    if turn is None:
        # Engine crashed/timed out — fall back to the old screen scrape so Listen
        # never goes fully dead.
        text = _capture_scrollback(lines=200, session_id=sid)
        if not text:
            return jsonify({"ok": False, "error": "Nothing to capture"}), 400
    elif turn.get("ok") and turn.get("assistant"):
        text = _turn_to_text(turn)
        logging.info(f"LISTEN ({mode}) via transcript {turn.get('file')}")
    else:
        # Matched the tab but there's no answered turn yet (e.g. freshly cleared).
        return jsonify({"ok": False, "error": "Nothing to read yet"}), 400
    tmp, win_tmp = _write_scrollback(text)

    # Clean up mp3s from previous listens
    # Piper writes .wav, gTTS writes .mp3 — sweep both or the wavs pile up.
    for old in [*_SPARK_DIR.glob("_listen_*.mp3"), *_SPARK_DIR.glob("_listen_*.wav")]:
        try: old.unlink()
        except Exception: pass

    job_id = str(uuid.uuid4())[:8]
    _text_jobs[job_id] = "pending"
    with _listen_guard_lock:
        _listen_recent[guard_key] = {"job": job_id, "started": time.time()}

    def _do():
        try:
            # Piper path: notify.py returns TEXT only and we synthesize here with
            # the warm voice. Falls back to the original all-in-notify.py gTTS
            # route whenever the voice is not loaded, so this can never be the
            # reason Listen stops working.
            with _piper_lock:
                use_piper = _piper_voice is not None
            run_mode = (mode + "_text") if use_piper else mode

            result = subprocess.run(
                [_WIN_PYTHON, "C:/dev/spark/notify.py", run_mode, win_tmp],
                capture_output=True, timeout=90,
                encoding="utf-8", errors="replace",
            )
            mp3 = None
            for line in (result.stdout or "").splitlines():
                if line.startswith("MP3:"):
                    mp3 = line[4:].strip()
                elif line.startswith("SUMMARY:") and use_piper:
                    summary = json.loads(line[8:])
                    wav = _SPARK_DIR / f"_listen_{job_id}.wav"
                    if _piper_say(summary, wav):
                        mp3 = str(wav)
                    else:
                        logging.warning("PIPER: synth failed, no audio produced")
            if result.returncode == 0 and mp3:
                _t_ready = time.time() - _job_start.get(job_id, time.time())
                for line in (result.stdout or "").splitlines():
                    if line.startswith("NOTIFY_T:"):   # notify.py's own split
                        logging.info(f"LISTEN_T job={job_id} {line[9:].strip()}")
                logging.info(f"LISTEN_T job={job_id} mp3_ready={_t_ready:.2f}s "
                             f"after button press")
                logging.info(f"LISTEN OK: {mp3}")
                _audio_files[job_id] = mp3
                _text_jobs[job_id] = "ready"
            else:
                logging.error(f"LISTEN_ERR: {result.stderr.strip()}")
                _text_jobs[job_id] = "failed"
        except subprocess.TimeoutExpired:
            logging.error("LISTEN: timed out after 90s")
            _text_jobs[job_id] = "timeout"
        except Exception as e:
            logging.error(f"LISTEN: {e}")
            _text_jobs[job_id] = "failed"
        finally:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

    _t_sync = time.time() - _t0
    logging.info(f"LISTEN_T job={job_id} mode={mode} chars={len(text)} "
                 f"sync_total={_t_sync:.2f}s <- client BLOCKED this long before "
                 f"polling even starts")
    _job_start[job_id] = _t0
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "job": job_id})


@app.route("/api/listen-audio/<job_id>")
def listen_audio(job_id):
    path = _audio_files.get(job_id)
    if not path or not Path(path).exists():
        return jsonify({"error": "Audio not found"}), 404
    mime = "audio/wav" if str(path).lower().endswith(".wav") else "audio/mpeg"
    return send_file(path, mimetype=mime)


@app.route("/api/retry", methods=["POST"])
def retry():
    if not _last_text:
        return jsonify({"error": "Nothing to retry"}), 400
    send_to_claude(_last_text)
    return jsonify({"ok": True, "text": _last_text})


@app.route("/api/voice-text", methods=["POST"])
def voice_text():
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    send_to_claude(text, session_id=data.get("session"))
    return jsonify({"ok": True, "input": text})


@app.route("/api/paste-text", methods=["POST"])
def paste_text():
    """Paste text into tmux without hitting Enter — lets user accumulate input.
    Uses set-buffer + paste-buffer to handle long text reliably
    (send-keys -l truncates at ~500 chars)."""
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    global _last_text
    _last_text = text
    tmux = _resolve_session(data)
    # Load text into tmux paste buffer, then paste it — no length limit.
    # Both in one round trip; the buffer must be set before the paste, and the
    # helper runs a batch strictly in order.
    _tmux_run_many(
        ["set-buffer", "--", _tmux_literal(text)],
        ["paste-buffer", "-t", tmux],
    )
    logging.info(f"PASTE text='{text[:80]}' ({len(text)} chars) session={tmux} (no enter)")
    return jsonify({"ok": True, "input": text})


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file"}), 400
    audio_file = request.files["audio"]
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "Empty audio"}), 400
    logging.info(f"[Spark] TRANSCRIBE: {len(audio_bytes)} bytes")
    try:
        text = transcribe_audio(audio_bytes, filename=audio_file.filename or "recording.webm")
        logging.info(f"[Spark] TRANSCRIBE: '{text[:100]}'")
        return jsonify({"text": text})
    except Exception as e:
        logging.info(f"[Spark] TRANSCRIBE ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/text-status/<job_id>")
def text_status(job_id):
    status = _text_jobs.get(job_id, "unknown")
    return jsonify({"status": status})


@app.route("/api/log", methods=["POST"])
def client_log():
    data = request.get_json()
    msg = data.get("msg", "")
    logging.info(f"[CLIENT] {msg}")
    return jsonify({"ok": True})


def _kill_port(port):
    """Kill whatever is holding the port so we can restart cleanly."""
    try:
        if _IS_WINDOWS:
            out = subprocess.check_output(
                ["powershell", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue).OwningProcess"],
                text=True, timeout=5,
            ).strip()
            for pid in set(out.splitlines()):
                pid = pid.strip()
                if pid and pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/F", "/PID", pid, "/T"],
                                   capture_output=True, timeout=5)
                    print(f"[Spark] Killed old process on port {port} (PID {pid})")
        else:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{port}"], text=True, timeout=5,
            ).strip()
            for pid in set(out.splitlines()):
                pid = pid.strip()
                if pid and pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                    print(f"[Spark] Killed old process on port {port} (PID {pid})")
    except Exception:
        pass


_ctrl_c_count = 0

def _handle_sigint(sig, frame):
    global _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count >= 2:
        print("\n[Spark] Force quit.")
        os._exit(1)
    print("\n[Spark] Ctrl+C again to force quit.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sigint)
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        _kill_port(PORT)
    if not SPARK_TOKEN:
        where = PUBLIC_HOST or "the tunnel hostname"
        print(f"[Spark] WARNING: SPARK_TOKEN not set in .env — API is "
              f"UNPROTECTED and {where} is public!")
    print(f"[Spark] Voice layer on port {PORT}")
    app.run(host=HOST, port=PORT, debug=False)
