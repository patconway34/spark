"""Spark — voice layer for Claude Code via tmux/ttyd.

Embeds ttyd terminal in an iframe. Adds voice input (browser speech recognition)
and voice output (gTTS from transcript JSONL).
"""

import json
import logging
import os
import subprocess
from datetime import datetime
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, send_file

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
_active_session = SESSIONS[0]


def get_session():
    return _active_session

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
    """Send keystrokes to the active tmux session."""
    global _last_text
    _last_text = text
    tmux = get_session()["tmux"]
    cmd = text.strip().lower().rstrip(".")
    key = VOICE_COMMANDS.get(cmd)
    if key:
        result = subprocess.run(
            ["wsl", "bash", "-c", f"tmux send-keys -t {tmux} {key}"],
            capture_output=True, timeout=15,
        )
        logging.info(f"KEY cmd='{cmd}' session={tmux} rc={result.returncode} err={result.stderr.decode().strip()}")
    else:
        escaped = text.replace("'", "'\\''")
        result = subprocess.run(
            ["wsl", "bash", "-c", f"tmux send-keys -t {tmux} '{escaped}' Enter"],
            capture_output=True, timeout=15,
        )
        logging.info(f"SEND text='{text[:80]}' session={tmux} rc={result.returncode} err={result.stderr.decode().strip()}")


@app.route("/")
@app.route("/chat")
def index():
    resp = make_response(render_template("chat.html", session=_active_session))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"sessions": SESSIONS, "active": _active_session["id"]})


@app.route("/api/session", methods=["POST"])
def set_session():
    global _active_session
    data = request.get_json()
    sid = data.get("id", "")
    for s in SESSIONS:
        if s["id"] == sid:
            _active_session = s
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
            _save_sessions()
            logging.info(f"SESSION_RENAME {sid} -> {name}")
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


@app.route("/api/key", methods=["POST"])
def key():
    """Send a raw key to the tmux session."""
    data = request.get_json()
    k = (data.get("key") or "").strip()
    if not k:
        return jsonify({"error": "No key"}), 400
    tmux = get_session()["tmux"]
    subprocess.run(
        ["wsl", "bash", "-c", f"tmux send-keys -t {tmux} {k}"],
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
        cmd = f"tmux copy-mode -t {tmux} 2>/dev/null; tmux send-keys -t {tmux} -X page-up 2>/dev/null"
    else:
        cmd = f"tmux send-keys -t {tmux} -X cancel 2>/dev/null"
    subprocess.run(["wsl", "bash", "-c", cmd], capture_output=True, timeout=15)
    return jsonify({"ok": True})


@app.route("/api/retry", methods=["POST"])
def retry():
    """Resend the last transcribed text."""
    if not _last_text:
        return jsonify({"error": "Nothing to retry"}), 400
    logging.info(f"[Spark] RETRY text='{_last_text[:80]}'")
    send_to_claude(_last_text)
    return jsonify({"ok": True, "text": _last_text})


@app.route("/api/voice-text", methods=["POST"])
def voice_text():
    """Receive already-transcribed text, send to claude."""
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
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
            ["wsl", "bash", "-c", f"tmux capture-pane -t {get_session()['tmux']} -p"],
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


@app.route("/api/screen-status")
def screen_status():
    """Return Claude's current state: working, idle, or asking a question."""
    import re
    raw = _capture_pane()
    if not raw:
        return jsonify({"status": "unknown"})

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
        return jsonify({"status": "question", "text": "\n".join(q_lines)})

    # Check for working indicators
    working_patterns = re.compile(
        r'(Running|Flibbert|thinking|Searching|Reading|Writing|Editing'
        r'|tokens|◐|◑|◒|◓|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|✻|\*.*…)',
        re.IGNORECASE
    )
    if working_patterns.search(last_lines):
        return jsonify({"status": "working"})

    return jsonify({"status": "idle"})


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


@app.route("/api/screen-latest")
def screen_latest():
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
        return jsonify({"text": None, "hash": None, "status": "no_content"})

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
        return jsonify({"text": None, "hash": None, "status": "no_content"})

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
        return jsonify({"text": None, "hash": None, "status": "changing"})

    # Check for Claude's input prompt — definitive "done" signal
    raw_lines = [l for l in raw_content.splitlines() if l.strip()]
    has_prompt = any('❯' in l or l.strip().endswith('>') for l in raw_lines[-3:]) if raw_lines else False

    # If we see the prompt, only need 2 stable polls. Otherwise need full threshold.
    needed = 2 if has_prompt else SCREEN_IDLE_POLLS
    if state["stable_count"] < needed:
        return jsonify({"text": None, "hash": None, "status": "settling"})

    # Grab the last N non-empty lines
    lines = [l for l in content.splitlines() if l.strip()]
    tail = lines[-SCREEN_TAIL_LINES:] if lines else []
    text = "\n".join(tail)

    if not text.strip():
        return jsonify({"text": None, "hash": h})

    # Compare actual text, not hash — skip if same as last spoken
    if text.strip() == state.get("last_spoken_text", ""):
        return jsonify({"text": None, "hash": h, "status": "already_spoken"})

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
    return jsonify({"text": spoken, "hash": h})


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


@app.route("/api/tts", methods=["POST"])
def tts():
    """Convert text to speech, return MP3."""
    data = request.get_json()
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    gTTS(text=text, lang="en", slow=False).save(tmp.name)
    return send_file(tmp.name, mimetype="audio/mpeg", as_attachment=False)


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


if __name__ == "__main__":
    _kill_port(PORT)
    print(f"[Spark] Voice layer on port {PORT}")
    print(f"[Spark] tmux session: {TMUX_SESSION}")
    app.run(host=HOST, port=PORT, debug=False)
