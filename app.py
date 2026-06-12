"""Spark — voice layer for Claude Code via tmux/ttyd.

Embeds ttyd terminal in an iframe. Adds voice input (browser speech recognition)
and voice output (gTTS from transcript JSONL).
"""

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
from datetime import datetime
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, send_file

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from gtts import gTTS
from transcribe import transcribe_audio

app = Flask(__name__)

# File logger so we can diagnose from WSL
_SPARK_DIR = Path(__file__).resolve().parent
LOG_FILE = _SPARK_DIR / "spark.log"
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                    handlers=[
                        logging.FileHandler(str(LOG_FILE)),
                        logging.StreamHandler(),
                    ])
PORT = 5023
HOST = "0.0.0.0"

# Sessions config — persisted to sessions.json
SESSIONS_FILE = _SPARK_DIR / "sessions.json"

DEFAULT_SESSIONS = [
    {"id": "dev", "name": "Dev", "tmux": "claude", "ttyd_port": 7682,
     "terminal_url": "https://terminal.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
    {"id": "nimbus", "name": "Nimbus", "tmux": "claude2", "ttyd_port": 7683,
     "terminal_url": "https://terminal2.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
    {"id": "alpha", "name": "Alpha", "tmux": "claude3", "ttyd_port": 7684,
     "terminal_url": "https://terminal3.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
    {"id": "bravo", "name": "Bravo", "tmux": "claude4", "ttyd_port": 7685,
     "terminal_url": "https://terminal4.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
    {"id": "charlie", "name": "Charlie", "tmux": "claude5", "ttyd_port": 7686,
     "terminal_url": "https://terminal5.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
    {"id": "delta", "name": "Delta", "tmux": "claude6", "ttyd_port": 7687,
     "terminal_url": "https://terminal6.tradingdata.net",
     "transcript_dir": "//wsl.localhost/Ubuntu/home/patrick/.claude/projects/-mnt-c-dev"},
]


def _load_sessions():
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return [dict(s) for s in DEFAULT_SESSIONS]


def _save_sessions():
    SESSIONS_FILE.write_text(json.dumps(SESSIONS, indent=2), encoding="utf-8")


SESSIONS = _load_sessions()
# Ensure every session has a last_used timestamp
for _s in SESSIONS:
    _s.setdefault("last_used", None)
_active_session = SESSIONS[0]


def get_session():
    return _active_session


def _touch_session(session):
    """Update last_used timestamp for a session."""
    session["last_used"] = datetime.now().isoformat()
    _save_sessions()

TMUX_SESSION = "claude"  # legacy reference, use get_session()["tmux"] instead

SETTLE_SECONDS = 3

# Locked transcript path per session (set on switch)
_locked_transcripts = {}

# Screen capture state per session for idle detection
_screen_state = {}  # session_id -> {"hash": str, "stable_count": int, "last_content": str, "last_spoken_hash": str}
SCREEN_IDLE_POLLS = 4  # consecutive unchanged polls before declaring idle (~12s)
SCREEN_TAIL_LINES = 15  # how many lines from bottom to grab for TTS
TRANSCRIPT_DIR = _SPARK_DIR / "transcripts"
TRANSCRIPT_DIR.mkdir(exist_ok=True)


def _append_transcript(session_id, text, role="screen"):
    """Append an entry to the session's transcript file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = TRANSCRIPT_DIR / f"{session_id}.jsonl"
    # Strip "thank you" filler from user messages
    if role == "user":
        import re
        text = re.sub(r'^(thank you[\.\,\s]*)+', '', text, flags=re.IGNORECASE).strip()
        if not text:
            return
    entry = json.dumps({"ts": ts, "role": role, "text": text}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


# Track last screen entry per session for merging
_last_screen_entry = {}


def _word_set(text):
    """Get set of words for similarity comparison."""
    return set(text.lower().split())


def _similarity(a, b):
    """Word-level Jaccard similarity (0-1)."""
    sa, sb = _word_set(a), _word_set(b)
    if not sa or not sb:
        return 0
    return len(sa & sb) / len(sa | sb)


def _append_or_merge_transcript(session_id, text):
    """Append screen entry, skipping if too similar to recent entry."""
    now = datetime.now()
    last = _last_screen_entry.get(session_id)

    if last:
        time_gap = (now - last["time"]).total_seconds()
        sim = _similarity(last["text"], text)
        # Skip if similar content within 30 seconds
        if sim > 0.4 and time_gap < 30:
            return
        # Replace if very similar and within 60 seconds (updated version of same response)
        if sim > 0.6 and time_gap < 60:
            path = TRANSCRIPT_DIR / f"{session_id}.jsonl"
            _last_screen_entry[session_id] = {"time": now, "text": text}
            if path.exists():
                lines = path.read_text(encoding="utf-8").strip().splitlines()
                if lines:
                    lines[-1] = json.dumps({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "role": "screen", "text": text}, ensure_ascii=False)
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    _last_screen_entry[session_id] = {"time": now, "text": text}
    _append_transcript(session_id, text)


def lock_transcript(session):
    """Detect and lock the transcript file for a session. Called on switch."""
    transcript_dir = Path(session["transcript_dir"])
    candidates = list(transcript_dir.glob("*.jsonl"))
    if candidates:
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        _locked_transcripts[session["id"]] = latest
        logging.info(f"TRANSCRIPT_LOCK {session['id']} -> {latest.name}")
    return _locked_transcripts.get(session["id"])


def find_latest_transcript():
    """Return the locked transcript for the active session."""
    sid = get_session()["id"]
    locked = _locked_transcripts.get(sid)
    if locked and locked.exists():
        return locked
    # Fallback: lock it now
    return lock_transcript(get_session())


def is_settled():
    """Return True if the transcript file hasn't been modified in SETTLE_SECONDS."""
    transcript = find_latest_transcript()
    if not transcript:
        return False
    age = time.time() - transcript.stat().st_mtime
    return age >= SETTLE_SECONDS




CHAT_FILE = _SPARK_DIR / "last_response.txt"
SUMMARY_FILE = _SPARK_DIR / "chat_summary.txt"
FULL_LOG = _SPARK_DIR / "chat_full.txt"

# Dedup tracking
_last_summary_hash = None


def get_last_assistant_text():
    """Read the latest assistant message from the transcript."""
    transcript = find_latest_transcript()
    if not transcript or not transcript.exists():
        return None, None
    lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content", [])
        texts = [
            c["text"]
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
        ]
        if texts:
            full = " ".join(texts)
            return full, str(hash(full))
    return None, None


def build_chat_transcript():
    """Build a chat-like transcript from the JSONL and write to file."""
    transcript = find_latest_transcript()
    if not transcript or not transcript.exists():
        return
    lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    chat = []
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = d.get("type")
        if role == "human":
            content = d.get("message", {}).get("content", [])
            texts = [
                c["text"] for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ]
            if texts:
                chat.append(f"Patrick: {' '.join(texts)}")
        elif role == "assistant":
            content = d.get("message", {}).get("content", [])
            texts = [
                c["text"] for c in content
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ]
            if texts:
                chat.append(f"Claude: {' '.join(texts)}")
    CHAT_FILE.write_text("\n\n".join(chat), encoding="utf-8")


def append_summary(text, is_assistant=False):
    """Append a line to the clean summary transcript. Deduped for assistant."""
    global _last_summary_hash
    if is_assistant:
        h = str(hash(text))
        if h == _last_summary_hash:
            return
        _last_summary_hash = h
    label = "Claude" if is_assistant else "Patrick"
    line = f"{label}: {text}\n\n"
    with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def build_full_log():
    """Build a detailed timestamped log from the JSONL transcript."""
    transcript = find_latest_transcript()
    if not transcript or not transcript.exists():
        return
    raw_lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    log = []
    for raw in raw_lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = d.get("timestamp", "")
        short_ts = ts[11:19] if len(ts) >= 19 else ""  # HH:MM:SS
        msg_type = d.get("type", "")

        if msg_type == "user" and not d.get("isMeta"):
            content = d.get("message", {}).get("content", [])
            if isinstance(content, str):
                text = content.strip()
            else:
                text = " ".join(
                    c["text"] for c in content
                    if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
                )
            if text and not text.startswith("<"):
                log.append(f"[{short_ts}] Patrick: {text}")

        elif msg_type == "assistant":
            content = d.get("message", {}).get("content", [])
            texts = []
            tools = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
                elif c.get("type") == "tool_use":
                    name = c.get("name", "?")
                    inp = c.get("input", {})
                    # Show what the tool is targeting
                    target = ""
                    if name == "Read" or name == "Edit" or name == "Write":
                        target = inp.get("file_path", "")
                    elif name == "Bash":
                        target = inp.get("command", "")[:80]
                    elif name == "Grep":
                        target = inp.get("pattern", "")
                    elif name == "Agent":
                        target = inp.get("description", "")
                    if target:
                        tools.append(f"  -> {name}: {target}")
                    else:
                        tools.append(f"  -> {name}")
            if texts:
                log.append(f"[{short_ts}] Claude: {' '.join(texts)}")
            for t in tools:
                log.append(f"[{short_ts}] {t}")

        elif msg_type == "progress":
            data = d.get("data", {})
            sub = data.get("type", "")
            if sub == "agent_progress":
                msg = data.get("message", {})
                inner_type = msg.get("type", "")
                if inner_type == "assistant":
                    inner_content = msg.get("message", {}).get("content", [])
                    for c in inner_content:
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                            snippet = c["text"][:150]
                            log.append(f"[{short_ts}]   (agent) {snippet}")

        elif msg_type == "system":
            content = d.get("content", "")
            if content and not content.startswith("<"):
                log.append(f"[{short_ts}] System: {content[:150]}")

    FULL_LOG.write_text("\n".join(log), encoding="utf-8")



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


_last_text = None

def send_to_claude(text):
    """Send keystrokes to the active tmux session. No shell — argument lists only."""
    global _last_text
    _last_text = text
    tmux = get_session()["tmux"]
    cmd = text.strip().lower().rstrip(".")
    key = VOICE_COMMANDS.get(cmd)
    if key:
        result = subprocess.run(
            ["wsl", "tmux", "send-keys", "-t", tmux, *key.split()],
            capture_output=True, timeout=15,
        )
        logging.info(f"KEY cmd='{cmd}' session={tmux} rc={result.returncode} err={result.stderr.decode().strip()}")
    else:
        # -l = literal text, -- stops option parsing; then Enter as a separate key
        result = subprocess.run(
            ["wsl", "tmux", "send-keys", "-t", tmux, "-l", "--", text],
            capture_output=True, timeout=15,
        )
        subprocess.run(
            ["wsl", "tmux", "send-keys", "-t", tmux, "Enter"],
            capture_output=True, timeout=15,
        )
        logging.info(f"SEND text='{text[:80]}' session={tmux} rc={result.returncode} err={result.stderr.decode().strip()}")


@app.route("/")
@app.route("/chat")
def index():
    resp = make_response(render_template("chat.html", session=_active_session))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/talk")
def talk():
    """Chat-first view: session status feed + question queue, no terminal."""
    resp = make_response(render_template("talk.html", session=_active_session))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/blink")
def blink():
    return redirect("https://blink.tradingdata.net")


def _pane_info():
    """Return {tmux_session: {"cmd": ..., "path": ...}} for all sessions in one tmux call."""
    try:
        result = subprocess.run(
            ["wsl", "tmux", "list-panes", "-a", "-F",
             "#{session_name}\t#{pane_current_command}\t#{pane_current_path}"],
            capture_output=True, timeout=10, text=True,
        )
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
    """Return {tmux_session: current_command} for all sessions."""
    return {k: v["cmd"] for k, v in _pane_info().items()}


_SHELLS = {"bash", "sh", "zsh", "fish", "dash"}


@app.route("/api/sessions")
def api_sessions():
    cmds = _pane_commands()
    feed = {f["id"]: f for f in _feed_state["sessions"]}
    out = []
    for s in SESSIONS:
        d = dict(s)
        cmd = cmds.get(s["tmux"])
        d["running"] = cmd
        d["alive"] = bool(cmd) and cmd not in _SHELLS
        d["small"] = feed.get(s["id"], {}).get("small", "")
        d["status"] = feed.get(s["id"], {}).get("status", "")
        out.append(d)
    return jsonify({"sessions": out, "active": _active_session["id"]})


LAUNCH_COMMANDS = {
    "claude": "claude --model claude-opus-4-6 --effort medium",
    "gemini": "gemini",
    "chatgpt": "codex",
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
    for s in SESSIONS:
        if s["id"] == sid:
            tmux = s["tmux"]
            subprocess.run(["wsl", "tmux", "respawn-pane", "-k", "-t", tmux],
                           capture_output=True, timeout=15)
            time.sleep(0.5)
            subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux,
                            f"cd /mnt/c/dev && {cmd}", "Enter"],
                           capture_output=True, timeout=15)
            logging.info(f"LAUNCH {cli} in {tmux} ({cmd})")
            return jsonify({"ok": True, "cmd": cmd})
    return jsonify({"error": "Unknown session"}), 400


@app.route("/api/session", methods=["POST"])
def set_session():
    global _active_session
    data = request.get_json()
    sid = data.get("id", "")
    for s in SESSIONS:
        if s["id"] == sid:
            _active_session = s
            _touch_session(s)
            lock_transcript(s)
            logging.info(f"SESSION_SWITCH to {s['name']} (tmux={s['tmux']})")
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
            s["custom_name"] = True  # manual rename wins — auto-rename leaves it alone
            _save_sessions()
            logging.info(f"SESSION_RENAME {sid} -> {name}")
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


TAB_DEFAULTS = {"dev": "1", "nimbus": "2", "alpha": "3",
                "bravo": "4", "charlie": "5", "delta": "6"}

# How to start a fresh conversation per CLI (by pane_current_command)
RESET_COMMANDS = {"claude": "/new", "codex": "/new", "gemini": "/clear", "node": "/clear"}


@app.route("/api/session/hide", methods=["POST"])
def hide_session():
    """Hide a session: drop from feed, reset name to its number, reset the conversation."""
    data = request.get_json()
    sid = data.get("id", "")
    hidden = bool(data.get("hidden", True))
    for s in SESSIONS:
        if s["id"] == sid:
            s["hidden"] = hidden
            if hidden:
                s["name"] = TAB_DEFAULTS.get(sid, s["name"])
                s.pop("custom_name", None)
                _folder_cache.pop(sid, None)
                _big_cache.pop(sid, None)
                cmd = _pane_commands().get(s["tmux"], "")
                reset = RESET_COMMANDS.get(cmd)
                if reset:
                    subprocess.run(["wsl", "tmux", "send-keys", "-t", s["tmux"], "-l", "--", reset],
                                   capture_output=True, timeout=15)
                    time.sleep(0.5)
                    subprocess.run(["wsl", "tmux", "send-keys", "-t", s["tmux"], "Enter"],
                                   capture_output=True, timeout=15)
                logging.info(f"SESSION_HIDE {sid} reset='{reset}' cmd='{cmd}'")
            else:
                logging.info(f"SESSION_UNHIDE {sid}")
            _save_sessions()
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


@app.route("/api/session/topic", methods=["GET"])
def session_topic():
    """Extract a short topic label from the active tmux screen content."""
    import re
    raw = _capture_pane()
    if not raw:
        return jsonify({"topic": None})

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return jsonify({"topic": None})

    # Strategy 1: Look for project path in Claude Code prompt (e.g. "/mnt/c/dev/spark")
    for line in reversed(lines[-20:]):
        m = re.search(r'/(?:mnt/c/)?dev/(\w+)', line)
        if m:
            project = m.group(1)
            return jsonify({"topic": project})

    # Strategy 2: Look for "C:/dev/project" or "C:\dev\project" Windows paths
    for line in reversed(lines[-20:]):
        m = re.search(r'[Cc]:[/\\]dev[/\\](\w+)', line)
        if m:
            return jsonify({"topic": m.group(1)})

    # Strategy 3: Use first meaningful words from the last non-empty content
    # Skip common noise lines
    noise = re.compile(r'^(\$|>|#|claude|tokens|Running|Press|Allow|Deny|─|━|⎿|●|✻)', re.I)
    content_lines = [l for l in lines[-10:] if not noise.match(l)]
    if content_lines:
        # Take last meaningful line, truncate to ~20 chars
        last = content_lines[-1][:30].strip()
        # Remove special chars
        last = re.sub(r'[^\w\s]', '', last).strip()
        words = last.split()[:3]
        if words:
            return jsonify({"topic": " ".join(words)})

    return jsonify({"topic": None})


ALLOWED_KEYS = {
    "Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right",
    "Space", "PageUp", "PageDown", "C-c", "C-z", "C-d",
}


@app.route("/api/key", methods=["POST"])
def key():
    """Send a named key to the tmux session. Allowlisted keys only."""
    data = request.get_json()
    k = (data.get("key") or "").strip()
    if k not in ALLOWED_KEYS:
        return jsonify({"error": "Key not allowed"}), 400
    tmux = get_session()["tmux"]
    subprocess.run(
        ["wsl", "tmux", "send-keys", "-t", tmux, k],
        capture_output=True, timeout=15,
    )
    return jsonify({"ok": True})


@app.route("/api/scroll", methods=["POST"])
def scroll():
    """Scroll the tmux pane using copy-mode (page-up / page-down)."""
    data = request.get_json()
    direction = data.get("direction", "up")  # "up" or "down"
    tmux = get_session()["tmux"]
    if direction == "up":
        subprocess.run(["wsl", "tmux", "copy-mode", "-t", tmux], capture_output=True, timeout=15)
        subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "-X", "page-up"], capture_output=True, timeout=15)
    else:
        # Exit copy-mode entirely so terminal snaps back to live input
        subprocess.run(["wsl", "tmux", "copy-mode", "-q", "-t", tmux], capture_output=True, timeout=15)
    return jsonify({"ok": True})


@app.route("/api/retry", methods=["POST"])
def retry():
    """Resend the last transcribed text."""
    if not _last_text:
        return jsonify({"error": "Nothing to retry"}), 400
    logging.info(f"[Spark] RETRY text='{_last_text[:80]}'")
    send_to_claude(_last_text)
    return jsonify({"ok": True, "text": _last_text})


HEY_RE = re.compile(r'^hey[\s,]+(\w+)[\s,.!]+(.+)$', re.IGNORECASE | re.DOTALL)


def _match_hey(text):
    """Parse 'hey <session> <message>' — returns (session, message) or (None, None)."""
    m = HEY_RE.match(text.strip())
    if not m:
        return None, None
    name, message = m.group(1).lower(), m.group(2).strip()
    for s in SESSIONS:
        if s["name"].lower() == name or s["name"].lower().startswith(name):
            return s, message
    return None, None


@app.route("/api/voice-text", methods=["POST"])
def voice_text():
    """Receive already-transcribed text, send to claude."""
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400

    # "hey alpha ..." routes to that session — only at a still point
    target, message = _match_hey(text)
    if target and message:
        status = next((x["status"] for x in _feed_state["sessions"]
                       if x["id"] == target["id"]), "unknown")
        if status == "working":
            logging.info(f"HEY_BUSY {target['name']} text='{message[:60]}'")
            return jsonify({"ok": False, "busy": True, "routed": target["name"]})
        tmux = target["tmux"]
        subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "-l", "--", message],
                       capture_output=True, timeout=15)
        subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "Enter"],
                       capture_output=True, timeout=15)
        logging.info(f"HEY_SEND {target['name']} text='{message[:60]}'")
        return jsonify({"ok": True, "routed": target["name"], "input": message})

    send_to_claude(text)
    append_summary(text, is_assistant=False)
    # Summarize user input for clean transcript
    try:
        from summarize import ask
        user_summary = ask(
            prompt=f"Clean up this voice input into 1-2 clear sentences. Remove filler words, false starts, and repetition. Keep the meaning exact:\n\n{text[:1000]}",
            system="Output only the cleaned text. No preamble. Keep it in first person.",
        )
        if user_summary and len(user_summary.strip()) > 5:
            _append_transcript(get_session()["id"], user_summary.strip(), role="user")
        else:
            _append_transcript(get_session()["id"], text, role="user")
    except Exception:
        _append_transcript(get_session()["id"], text, role="user")
    return jsonify({"ok": True, "input": text})



@app.route("/api/latest")
def latest():
    """Return the latest assistant message from the transcript."""
    if not is_settled():
        return jsonify({"text": None, "hash": None, "status": "waiting"})

    build_chat_transcript()
    build_full_log()
    text, h = get_last_assistant_text()
    if not text:
        return jsonify({"text": None, "hash": None})

    # Short responses read directly, long ones go to Mente
    spoken = text
    if len(text) > 300:
        from summarize import ask
        spoken = ask(
            prompt=f"Summarize this in 1-2 short sentences for voice readback:\n\n{text[:2000]}",
            system="Output only the summary. Keep it conversational and under 30 words.",
        )

    # Append to summary transcript
    append_summary(spoken, is_assistant=True)

    return jsonify({"text": spoken, "hash": h})


@app.route("/api/full-log")
def full_log():
    """Return the full detailed log."""
    if FULL_LOG.exists():
        text = FULL_LOG.read_text(encoding="utf-8")
        # Return last 200 lines for the UI
        lines = text.strip().splitlines()
        tail = "\n".join(lines[-200:])
        return jsonify({"text": tail, "total_lines": len(lines)})
    return jsonify({"text": "", "total_lines": 0})


@app.route("/api/screen")
def screen():
    """Capture the tmux pane and check for prompts."""
    try:
        result = subprocess.run(
            ["wsl", "tmux", "capture-pane", "-t", get_session()["tmux"], "-p"],
            capture_output=True, timeout=15, text=True,
        )
        text = result.stdout.strip()
        # Look for permission/question prompts
        prompts = ["Allow", "allow", "y/n", "Y/n", "yes/no", "Press Enter", "Continue?",
                    "proceed", "Proceed", "(Y/n)", "(y/n)", "approve"]
        waiting = any(p in text for p in prompts)
        # Get last few non-empty lines for context
        lines = [l for l in text.splitlines() if l.strip()]
        tail = lines[-3:] if lines else []
        return jsonify({"waiting": waiting, "tail": tail})
    except Exception as e:
        return jsonify({"waiting": False, "tail": [], "error": str(e)})


def _screen_status_data():
    """Claude's current state: working, idle, or asking a question."""
    import re
    raw = _capture_pane()
    if not raw:
        return {"status": "unknown"}

    lines = raw.strip().splitlines()
    last_lines = "\n".join(lines[-10:]) if lines else ""

    # Check for permission/question prompts
    question_patterns = re.compile(
        r'(1\.\s*Yes|2\.\s*Yes|3\.\s*No|Do you want to proceed'
        r'|Allow|Deny|y/n|Y/n|yes/no|\(Y/n\)|\(y/n\)'
        r'|Press Enter|Continue\?|proceed\?|approve)',
        re.IGNORECASE
    )
    if question_patterns.search(last_lines):
        q_lines = [l.strip() for l in lines[-8:] if l.strip()]
        return {"status": "question", "text": "\n".join(q_lines)}

    # Check for working indicators
    working_patterns = re.compile(
        r'(Running|Flibbert|thinking|Searching|Reading|Writing|Editing'
        r'|tokens|◐|◑|◒|◓|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|✻|\*.*…)',
        re.IGNORECASE
    )
    if working_patterns.search(last_lines):
        return {"status": "working"}

    return {"status": "idle"}


@app.route("/api/screen-status")
def screen_status():
    return jsonify(_screen_status_data())


def _capture_pane():
    """Capture the current tmux pane content."""
    tmux = get_session()["tmux"]
    try:
        result = subprocess.run(
            ["wsl", "tmux", "capture-pane", "-t", tmux, "-p"],
            capture_output=True, timeout=15, encoding="utf-8", errors="replace",
        )
        return (result.stdout or "") if result.returncode == 0 else ""
    except Exception:
        return ""


# --- Multi-session question queue ---

QUESTION_RE = re.compile(
    r'(1\.\s*Yes|2\.\s*Yes|3\.\s*No|Do you want to proceed'
    r'|Allow|Deny|y/n|Y/n|yes/no|\(Y/n\)|\(y/n\)'
    r'|Press Enter|Continue\?|proceed\?|approve)',
    re.IGNORECASE
)

WORKING_RE = re.compile(
    r'(Running|Flibbert|thinking|Searching|Reading|Writing|Editing'
    r'|tokens|◐|◑|◒|◓|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|✻|\*.*…|esc to interrupt)',
    re.IGNORECASE
)

_recently_answered = {}  # question hash -> time answered (suppress until screen updates)
ANSWERED_TTL = 90  # seconds


def _capture_all_panes(tail=14):
    """Capture the last lines of every session's pane in one wsl call."""
    parts = []
    for s in SESSIONS:
        n = s["tmux"]
        parts.append(f'echo "===={n}===="; tmux capture-pane -t {n} -p 2>/dev/null | tail -n {tail}')
    script = "; ".join(parts)
    try:
        result = subprocess.run(
            ["wsl", "bash", "-c", script],
            capture_output=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except Exception:
        return {}
    panes = {}
    current = None
    for line in (result.stdout or "").splitlines():
        m = re.match(r'^====(\S+)====$', line)
        if m:
            current = m.group(1)
            panes[current] = []
        elif current is not None:
            panes[current].append(line)
    return {k: "\n".join(v) for k, v in panes.items()}


def _scan_sessions():
    """Scan every session's pane: status (question/working/idle) + open questions."""
    now = time.time()
    for h, t in list(_recently_answered.items()):
        if now - t > ANSWERED_TTL:
            del _recently_answered[h]

    panes = _capture_all_panes()
    statuses = []
    questions = []
    for s in SESSIONS:
        text = panes.get(s["tmux"], "")
        lines = [l for l in text.splitlines() if l.strip()]
        status = "unknown"
        if lines:
            last_lines = "\n".join(lines[-10:])
            if QUESTION_RE.search(last_lines):
                q_text = "\n".join(l.strip() for l in lines[-8:])
                h = hashlib.md5((s["id"] + q_text).encode("utf-8")).hexdigest()[:12]
                if h in _recently_answered:
                    status = "working"  # just answered, screen still settling
                else:
                    status = "question"
                    questions.append({"id": s["id"], "name": s["name"], "text": q_text, "hash": h})
            elif WORKING_RE.search(last_lines):
                status = "working"
            else:
                status = "idle"
        statuses.append({"id": s["id"], "name": s["name"], "status": status})
    return statuses, questions


@app.route("/api/questions")
def api_questions():
    statuses, questions = _scan_sessions()
    return jsonify({"questions": questions, "sessions": statuses})


# --- Live feed: big/small status per session, refreshed by background thread ---

_feed_state = {"sessions": [], "questions": [], "ts": 0}
_big_cache = {}  # sid -> {"big": str, "ts": float, "hash": str}
BIG_REFRESH_SECONDS = 60

SMALL_TOOL_RE = re.compile(r'\b(Bash|Read|Edit|Write|Grep|Glob|Agent)\(([^)]{0,60})')
FOLDER_RE = re.compile(r'(?:/mnt/c/dev/|[Cc]:[/\\]dev[/\\])([A-Za-z_]\w*)')

TOOL_VERBS = {
    "Bash": "running", "Read": "reading", "Edit": "editing", "Write": "writing",
    "Grep": "searching", "Glob": "searching", "Agent": "delegating",
}


_folder_cache = {}  # sid -> last non-empty folder list


def _derive_folders(sid, text):
    """Project folders this session is touching, most frequent first. Sticky.

    Frequency beats recency so a stray quoted path (e.g. another session's
    output shown on this screen) doesn't displace the folder actually in use.
    """
    counts = {}
    match_lines = []
    for line in text.splitlines():
        for m in FOLDER_RE.finditer(line):
            f = m.group(1).lower()
            counts[f] = counts.get(f, 0) + 1
            match_lines.append(line.strip()[:110])
    # Need 2+ mentions: one stray quoted path can't flip the card
    seen = sorted((f for f in counts if counts[f] >= 2),
                  key=lambda f: counts[f], reverse=True)[:3]
    if seen:
        if seen != _folder_cache.get(sid):
            logging.info(f"FOLDERS {sid} counts={counts} -> {seen} lines={match_lines[:4]}")
        _folder_cache[sid] = seen
        return seen, True  # fresh sighting
    return _folder_cache.get(sid, []), False  # cached only


def _derive_small(lines, status, q_text=""):
    """Short, fast-changing status line — what's happening right now."""
    if status == "question":
        first = q_text.splitlines()[0].strip() if q_text else ""
        return f"asking: {first[:70]}" if first else "asking a question"
    if status == "idle":
        return "idle — waiting for you"
    for l in reversed(lines):
        m = SMALL_TOOL_RE.search(l)
        if m:
            name, arg = m.group(1), m.group(2).strip()
            verb = TOOL_VERBS.get(name, name.lower())
            return f"{verb}: {arg}" if arg else verb
    return "working"


def _refresh_big(sid, content):
    """Slow-changing one-line summary of what the session is working on."""
    h = hashlib.md5(content.encode("utf-8")).hexdigest()
    cache = _big_cache.setdefault(sid, {"big": "", "ts": 0, "hash": ""})
    now = time.time()
    if cache["big"] and (now - cache["ts"] < BIG_REFRESH_SECONDS or cache["hash"] == h):
        return cache["big"]
    try:
        from summarize import ask
        big = ask(
            prompt=f"This is a terminal screen from a Claude Code session. In 1-2 short sentences, what is this session working on overall — the bigger goal, not the current step? Plain words, present tense, first person plural (we).\n\n{content[-2500:]}",
            system="Output only the sentences. Under 30 words total. No preamble.",
        )
        if big and len(big.strip()) > 3:
            cache.update({"big": big.strip(), "ts": now, "hash": h})
    except Exception:
        pass
    return cache["big"]


_CWD_FOLDER_RE = re.compile(r'^/mnt/c/dev/([A-Za-z_]\w*)', re.IGNORECASE)


def _cwd_folder(path):
    """Project folder from a pane's working directory ('' if at dev root or elsewhere)."""
    m = _CWD_FOLDER_RE.match(path or "")
    return m.group(1).lower() if m else ""


def _maybe_autorename(session, folder):
    """Name the tab after the folder the session is working in. Manual rename wins."""
    if not folder or session.get("custom_name"):
        return
    if session["name"].lower() != folder:
        session["name"] = folder
        _save_sessions()
        logging.info(f"SESSION_AUTORENAME {session['id']} -> {folder}")


def _feed_loop():
    """Background scanner: keeps _feed_state fresh so /api/feed is instant."""
    while True:
        try:
            info = _pane_info()
            panes = _capture_all_panes(tail=80)
            now = time.time()
            for h, t in list(_recently_answered.items()):
                if now - t > ANSWERED_TTL:
                    del _recently_answered[h]
            sessions_out = []
            questions = []
            for s in SESSIONS:
                if s.get("hidden"):
                    continue
                pane = info.get(s["tmux"], {})
                cmd = pane.get("cmd")
                alive = bool(cmd) and cmd not in _SHELLS
                text = panes.get(s["tmux"], "")
                lines = [l for l in text.splitlines() if l.strip()]
                if not alive or not lines:
                    sessions_out.append({"id": s["id"], "name": s["name"],
                                         "status": "inactive", "big": "", "small": "inactive",
                                         "folders": []})
                    continue
                last_lines = "\n".join(lines[-10:])
                q_text = ""
                if QUESTION_RE.search(last_lines):
                    q_text = "\n".join(l.strip() for l in lines[-8:])
                    qh = hashlib.md5((s["id"] + q_text).encode("utf-8")).hexdigest()[:12]
                    if qh in _recently_answered:
                        status = "working"
                    else:
                        status = "question"
                        questions.append({"id": s["id"], "name": s["name"],
                                          "text": q_text, "hash": qh})
                elif WORKING_RE.search(last_lines):
                    status = "working"
                else:
                    status = "idle"
                big = _refresh_big(s["id"], "\n".join(lines))
                small = _derive_small(lines, status, q_text)
                folders, fresh = _derive_folders(s["id"], text)
                # Pane working directory is the truth; screen sightings are the fallback
                cwd = _cwd_folder(pane.get("path"))
                if cwd:
                    folders = [cwd] + [f for f in folders if f != cwd]
                _maybe_autorename(s, cwd or (folders[0] if fresh and folders else ""))
                sessions_out.append({"id": s["id"], "name": s["name"],
                                     "status": status, "big": big, "small": small,
                                     "folders": folders})
            _feed_state.update({"sessions": sessions_out, "questions": questions, "ts": now})
        except Exception as e:
            logging.info(f"FEED_ERR {e}")
        time.sleep(5)


@app.route("/api/feed")
def api_feed():
    return jsonify(_feed_state)


@app.route("/api/send", methods=["POST"])
def api_send():
    """Send a new prompt to a specific session — only if it's at a still point."""
    data = request.get_json()
    sid = data.get("id", "")
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    session = next((s for s in SESSIONS if s["id"] == sid), None)
    if not session:
        return jsonify({"error": "Unknown session"}), 400
    # Use the live feed state if fresh, otherwise scan now
    if time.time() - _feed_state["ts"] < 15 and _feed_state["sessions"]:
        statuses = _feed_state["sessions"]
    else:
        statuses, _ = _scan_sessions()
    status = next((x["status"] for x in statuses if x["id"] == sid), "unknown")
    if status == "working":
        return jsonify({"error": "busy", "status": status}), 409
    tmux = session["tmux"]
    subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "-l", "--", text],
                   capture_output=True, timeout=15)
    subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "Enter"],
                   capture_output=True, timeout=15)
    logging.info(f"FEED_SEND {sid} ({status}) text='{text[:60]}'")
    return jsonify({"ok": True, "status": status})


QUEUE_KEYS = {"1", "2", "3", "Enter", "Escape", "Tab"}


@app.route("/api/questions/answer", methods=["POST"])
def answer_question():
    """Send an answer (key or text) to a specific session's tmux pane."""
    data = request.get_json()
    sid = data.get("id", "")
    h = data.get("hash", "")
    key = (data.get("key") or "").strip()
    text = (data.get("text") or "").strip()
    session = next((s for s in SESSIONS if s["id"] == sid), None)
    if not session:
        return jsonify({"error": "Unknown session"}), 400
    tmux = session["tmux"]
    if key:
        if key not in QUEUE_KEYS:
            return jsonify({"error": "Key not allowed"}), 400
        keys = [key, "Enter"] if key in {"1", "2", "3"} else [key]
        for k in keys:
            subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, k],
                           capture_output=True, timeout=15)
    elif text:
        subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "-l", "--", text],
                       capture_output=True, timeout=15)
        subprocess.run(["wsl", "tmux", "send-keys", "-t", tmux, "Enter"],
                       capture_output=True, timeout=15)
    else:
        return jsonify({"error": "No answer"}), 400
    if h:
        _recently_answered[h] = time.time()
    logging.info(f"QUEUE_ANSWER {sid} key='{key}' text='{text[:60]}'")
    return jsonify({"ok": True})


def _screen_latest_data():
    """Poll-based idle detection + screen capture for TTS.

    Returns new content only when the screen has stopped changing.
    Works with any terminal, not just Claude Code.
    """
    import hashlib
    sid = get_session()["id"]
    state = _screen_state.setdefault(sid, {
        "hash": "", "stable_count": 0, "last_content": "", "last_spoken_hash": "",
    })

    raw_content = _capture_pane()
    if not raw_content:
        return {"text": None, "hash": None, "status": "no_content"}

    # Strip spinners/status for stable hash detection
    import re
    skip_patterns = re.compile(
        r'(Running|Flibbert|tokens|ctrl\+|background|↓|↑|⎿|●|✻|\*.*…|^\s*$'
        r'|Bash\(|Read\(|Edit\(|Write\(|Grep\(|Glob\(|Agent\(|\.{3,}'
        r'|claude doctor|Auto-update|accept edits|shift\+tab|━|─)'
    )
    content_lines = [l for l in raw_content.splitlines() if not skip_patterns.search(l)]
    content = "\n".join(content_lines)

    if not content.strip():
        return {"text": None, "hash": None, "status": "no_content"}

    # Hash only the stable content
    h = hashlib.md5(content.encode()).hexdigest()

    if h == state["hash"]:
        # Screen unchanged — increment stable counter
        state["stable_count"] += 1
        logging.info(f"SCREEN stable_count={state['stable_count']} need={SCREEN_IDLE_POLLS}")
    else:
        # Screen changed — reset
        state["hash"] = h
        state["stable_count"] = 1
        state["last_content"] = content
        return {"text": None, "hash": None, "status": "changing"}

    # Check for Claude's input prompt — definitive "done" signal
    raw_lines = [l for l in raw_content.splitlines() if l.strip()]
    has_prompt = any('❯' in l or l.strip().endswith('>') for l in raw_lines[-3:]) if raw_lines else False

    # If we see the prompt, only need 2 stable polls. Otherwise need full threshold.
    needed = 2 if has_prompt else SCREEN_IDLE_POLLS
    if state["stable_count"] < needed:
        return {"text": None, "hash": None, "status": "settling"}

    # Grab the last N non-empty lines
    lines = [l for l in content.splitlines() if l.strip()]
    tail = lines[-SCREEN_TAIL_LINES:] if lines else []
    text = "\n".join(tail)

    if not text.strip():
        return {"text": None, "hash": h}

    # Compare actual text, not hash — skip if same as last spoken
    if text.strip() == state.get("last_spoken_text", ""):
        return {"text": None, "hash": h, "status": "already_spoken"}

    # Summarize long output for voice
    spoken = text
    if len(text) > 300:
        try:
            from summarize import ask
            spoken = ask(
                prompt=f"Summarize this terminal output in 1-2 short sentences for voice readback:\n\n{text[:2000]}",
                system="Output only the summary. Keep it conversational and under 30 words.",
            )
        except Exception:
            spoken = "\n".join(tail[-3:])

    state["last_spoken_text"] = text.strip()

    # Clean text for transcript
    transcript_skip = re.compile(
        r'(❯|^\s*\d+\.\s*(Yes|No)|Do you want to proceed|Esc to cancel|Tab to amend'
        r'|\? for shortcuts|Bad\s+\d|Fine\s+\d|Good\s+\d|Dismiss'
        r'|Command contains|command substitution|Bash command|Showing detailed'
        r'|^\s*(Allow|Deny)\s*$|capture-pane|esc to interrupt'
        r'|2>/dev/null|/dev/null|\.jsonl|\.py\b|\.txt\b|\.csv\b'
        r'|/mnt/c/|C:\\|echo\s+"|grep\s|tail\s|wc\s|cat\s|mkdir\s|rm\s'
        r'|medium\s+·|/effort|◐|◑)',
        re.IGNORECASE
    )
    # Code/diff pattern: lines starting with +/- (diffs), line numbers, or high code char density
    code_pattern = re.compile(
        r'(^\s*\d+\s*\+|^\s*[\+\-]\s*\w|^\s*\+|'
        r'def\s|return\s|import\s|class\s|function\s|const\s|let\s|var\s|'
        r'jsonify|subprocess|innerHTML|addEventListener|querySelector|'
        r'localStorage|getElementById|\.replace\(|\.split\(|\.join\(|'
        r'\{.*\}|=>|===|!==)',
        re.IGNORECASE
    )
    clean_lines = []
    for l in text.strip().splitlines():
        if not l.strip():
            continue
        if transcript_skip.search(l):
            continue
        if code_pattern.search(l):
            continue
        # Keep indented lines (Claude's response) — skip unindented (user echo, commands)
        if l.startswith("  ") or l.startswith("\t"):
            clean_lines.append(l.strip())
    clean_text = "\n".join(clean_lines).strip()

    # Skip fragments under 20 chars
    if len(clean_text) >= 20:
        # Summarize for transcript — distill to the key point
        try:
            from summarize import ask
            summary = ask(
                prompt=f"What is Claude saying here? Distill to 1-2 short sentences. If it's a question, keep the question and options exactly:\n\n{clean_text[:2000]}",
                system="Output only the summary. No preamble. Under 40 words. If there are numbered options, keep them.",
            )
            if summary and len(summary.strip()) > 10:
                _append_or_merge_transcript(sid, summary.strip())
        except Exception:
            _append_or_merge_transcript(sid, clean_text)

    logging.info(f"SCREEN_LATEST session={sid} lines={len(tail)} chars={len(spoken)}")
    return {"text": spoken, "hash": h}


@app.route("/api/screen-latest")
def screen_latest():
    return jsonify(_screen_latest_data())


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/api/events")
def events():
    """Server-sent events: pushes status, questions, and speech to the client."""
    from flask import Response, stream_with_context

    def gen():
        last_status = None
        last_question = ""
        last_speak_hash = ""
        tick = 0
        while True:
            try:
                status = _screen_status_data()
                st = status.get("status")
                if st != last_status:
                    last_status = st
                    yield _sse("status", {"status": st})
                if st == "question":
                    q = status.get("text", "")
                    if q and q != last_question:
                        last_question = q
                        yield _sse("question", {"text": q})

                # Screen readback check every 3rd tick (~3s, matches old poll rate)
                if tick % 3 == 0:
                    data = _screen_latest_data()
                    if data.get("text") and data.get("hash") != last_speak_hash:
                        last_speak_hash = data["hash"]
                        yield _sse("speak", {"text": data["text"], "hash": data["hash"]})
            except GeneratorExit:
                raise
            except Exception as e:
                logging.info(f"SSE_ERR: {e}")

            if tick % 15 == 0:
                yield ": keepalive\n\n"
            tick += 1
            time.sleep(1)

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/transcript")
def get_transcript():
    """Return the transcript for the active session."""
    sid = get_session()["id"]
    path = TRANSCRIPT_DIR / f"{sid}.jsonl"
    if not path.exists():
        return jsonify({"entries": []})
    entries = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return jsonify({"entries": entries[-50:]})


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    """Receive audio blob, transcribe via Groq Whisper, return text."""
    if "audio" not in request.files:
        logging.info("[Spark] TRANSCRIBE: no audio in request.files")
        return jsonify({"error": "No audio file"}), 400
    audio_file = request.files["audio"]
    audio_bytes = audio_file.read()
    if not audio_bytes:
        logging.info("[Spark] TRANSCRIBE: empty audio bytes")
        return jsonify({"error": "Empty audio"}), 400
    logging.info(f"[Spark] TRANSCRIBE: got {len(audio_bytes)} bytes, filename={audio_file.filename}")
    try:
        text = transcribe_audio(audio_bytes, filename=audio_file.filename or "recording.webm")
        logging.info(f"[Spark] TRANSCRIBE: result='{text[:100]}'")
        return jsonify({"text": text})
    except Exception as e:
        logging.info(f"[Spark] TRANSCRIBE ERROR: {e}")
        return jsonify({"error": str(e)}), 500


TTS_CACHE_DIR = _SPARK_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)
TTS_CACHE_MAX_FILES = 100
TTS_VOICE = "en-US-AndrewMultilingualNeural"  # natural neural voice, handles EN + ES


def _generate_tts(text, path):
    """Generate speech mp3. edge-tts (neural) first, gTTS fallback."""
    try:
        import edge_tts
        edge_tts.Communicate(text, TTS_VOICE).save_sync(str(path))
        return
    except Exception as e:
        logging.info(f"TTS edge-tts failed ({e}), falling back to gTTS")
    gTTS(text=text, lang="en", slow=False).save(str(path))


def _prune_tts_cache():
    """Keep only the most recent TTS files."""
    files = sorted(TTS_CACHE_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[TTS_CACHE_MAX_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass


@app.route("/api/tts", methods=["POST"])
def tts():
    """Convert text to speech, return MP3. Cached by text hash."""
    import hashlib
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400

    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    mp3 = TTS_CACHE_DIR / f"{h}.mp3"
    if not mp3.exists():
        _generate_tts(text, mp3)
        _prune_tts_cache()
    return send_file(str(mp3), mimetype="audio/mpeg", as_attachment=False)


@app.route("/api/log", methods=["POST"])
def client_log():
    """Receive client-side log messages."""
    data = request.get_json()
    msg = data.get("msg", "")
    logging.info(f"[CLIENT] {msg}")
    return jsonify({"ok": True})


@app.route("/test")
def test_page():
    return """<!DOCTYPE html><html><head><title>Spark Mic Test</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>body{background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;padding:20px;}
    button{padding:12px 24px;font-size:16px;margin:8px;border:none;border-radius:8px;cursor:pointer;}
    #log{white-space:pre-wrap;font-size:12px;margin-top:16px;padding:12px;background:#111;border-radius:8px;max-height:60vh;overflow-y:auto;}</style></head>
    <body><h2>Spark Mic Test</h2>
    <button onclick="startTest()" style="background:#2ecc71;color:#fff;">Start Recording (5s)</button>
    <button onclick="testTranscribe()" style="background:#7b6ba8;color:#fff;">Send to Groq</button>
    <button onclick="testSend()" style="background:#e04040;color:#fff;">Send Last to tmux</button>
    <div id="log">Ready. Tap "Start Recording" and speak for 5 seconds.</div>
    <script>
    let blob = null, lastText = '';
    const log = document.getElementById('log');
    function addLog(msg) { log.textContent += '\\n' + new Date().toLocaleTimeString() + ' ' + msg; log.scrollTop = log.scrollHeight; }

    async function startTest() {
        addLog('Requesting mic...');
        try {
            const stream = await navigator.mediaDevices.getUserMedia({audio: true});
            addLog('Mic granted. Recording 5s...');
            const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/mp4';
            addLog('MIME: ' + mimeType);
            const recorder = new MediaRecorder(stream, {mimeType});
            const chunks = [];
            recorder.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data); };
            recorder.onstop = () => {
                blob = new Blob(chunks, {type: mimeType});
                addLog('Recording done. Size: ' + blob.size + ' bytes');
                stream.getTracks().forEach(t => t.stop());
            };
            recorder.start();
            setTimeout(() => { recorder.stop(); addLog('Stopping recorder...'); }, 5000);
        } catch(e) {
            addLog('MIC ERROR: ' + e.message);
        }
    }

    async function testTranscribe() {
        if (!blob) { addLog('No recording yet. Tap Start first.'); return; }
        addLog('Sending ' + blob.size + ' bytes to /api/transcribe...');
        const ext = blob.type.includes('webm') ? 'webm' : 'm4a';
        const fd = new FormData();
        fd.append('audio', blob, 'test.' + ext);
        try {
            const res = await fetch('/api/transcribe', {method:'POST', body:fd});
            const data = await res.json();
            if (data.error) { addLog('GROQ ERROR: ' + data.error); }
            else { lastText = data.text; addLog('TRANSCRIBED: ' + data.text); }
        } catch(e) { addLog('FETCH ERROR: ' + e.message); }
    }

    async function testSend() {
        if (!lastText) { addLog('No text yet. Transcribe first.'); return; }
        addLog('Sending to tmux: ' + lastText);
        try {
            const res = await fetch('/api/voice-text', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:lastText})});
            const data = await res.json();
            addLog('RESULT: ' + JSON.stringify(data));
        } catch(e) { addLog('SEND ERROR: ' + e.message); }
    }
    </script></body></html>"""


def _kill_port(port):
    """Kill whatever is holding the port so we can restart cleanly."""
    try:
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
    import threading
    signal.signal(signal.SIGINT, _handle_sigint)
    _kill_port(PORT)
    threading.Thread(target=_feed_loop, daemon=True).start()
    print(f"[Spark] Voice layer on port {PORT}")
    print(f"[Spark] tmux session: {TMUX_SESSION}")
    app.run(host=HOST, port=PORT, debug=False)
