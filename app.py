"""Spark — voice layer for Claude Code via tmux/ttyd.

Embeds ttyd terminal in an iframe. Sends voice input (Groq Whisper STT)
as keystrokes into tmux sessions.
"""

import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import platform

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, send_file

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
# Spark is publicly tunneled (spark.tradingdata.net) and its API injects
# keystrokes into live terminals. Every request must carry SPARK_TOKEN,
# either as ?token=... (first visit — sets a cookie), the cookie, or an
# X-Spark-Token header.

SPARK_TOKEN = os.getenv("SPARK_TOKEN", "")


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


# Three terminals (2026-08-02): one swipe each way reaches the other two, and all
# three stay loaded so switching is instant. tmux spark4-15 may still be running
# in the background (their Claude sessions), just not surfaced here.
_TERMINAL_COUNT = 3
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

# Terminal names live in terminal_names.txt (format: N=name, blank = number).
# Hot-reloaded on every /api/sessions poll so edits show up on refresh.
_NAMES_FILE = _SPARK_DIR / "terminal_names.txt"


def _load_terminal_names():
    """Return {id: name} for non-blank entries in terminal_names.txt."""
    names = {}
    try:
        for line in _NAMES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            sid, _, name = line.partition("=")
            sid, name = sid.strip(), name.strip()
            if sid and name:
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


@app.route("/")
def home():
    _apply_terminal_names()
    _apply_terminal_colors()
    active = get_session()
    resp = make_response(render_template("chat.html",
        session=active, sessions=SESSIONS, theme_ui=_theme_ui_for_template(),
        key_debug=_KEYDEBUG_FLAG.exists()))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


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
    _apply_terminal_names()
    _apply_terminal_colors()
    cmds = _pane_commands()
    out = []
    for s in SESSIONS:
        d = dict(s)
        cmd = cmds.get(s["tmux"])
        d["running"] = cmd
        d["alive"] = bool(cmd) and cmd not in _SHELLS
        out.append(d)
    return jsonify({"sessions": out, "active": _active_session_id})


# Verified against the live Models API 2026-07-25 — claude-opus-5 is real.
CLAUDE_MODELS = {
    "fable": "claude-fable-5",
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
        # Default model = the tab's own model (1-5 fable, 6-8 opus, 9 sonnet);
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
            work_dir = "/mnt/c/dev"
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
        return jsonify({"error": "No name"}), 400
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
}


def _resolve_session(data=None):
    """Resolve tmux session name from request data or fall back to active."""
    sid = (data or {}).get("session", "")
    if sid:
        for s in SESSIONS:
            if s["id"] == sid:
                return s["tmux"]
    return get_session()["tmux"]


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
    for old in _SPARK_DIR.glob("_listen_*.mp3"):
        try: old.unlink()
        except Exception: pass

    job_id = str(uuid.uuid4())[:8]
    _text_jobs[job_id] = "pending"

    def _do():
        try:
            result = subprocess.run(
                [_WIN_PYTHON, "C:/dev/spark/notify.py", mode, win_tmp],
                capture_output=True, timeout=90,
                encoding="utf-8", errors="replace",
            )
            mp3 = None
            for line in (result.stdout or "").splitlines():
                if line.startswith("MP3:"):
                    mp3 = line[4:].strip()
            if result.returncode == 0 and mp3:
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

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "job": job_id})


@app.route("/api/listen-audio/<job_id>")
def listen_audio(job_id):
    path = _audio_files.get(job_id)
    if not path or not Path(path).exists():
        return jsonify({"error": "Audio not found"}), 404
    return send_file(path, mimetype="audio/mpeg")


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
        print("[Spark] WARNING: SPARK_TOKEN not set in .env — API is UNPROTECTED "
              "and spark.tradingdata.net is public!")
    print(f"[Spark] Voice layer on port {PORT}")
    app.run(host=HOST, port=PORT, debug=False)
