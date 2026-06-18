"""Spark — voice layer for Claude Code via tmux/ttyd.

Embeds ttyd terminal in an iframe. Sends voice input (Groq Whisper STT)
as keystrokes into tmux sessions.
"""

import hashlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
import platform

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from transcribe import transcribe_audio

# --- App setup ---

app = Flask(__name__)
app.secret_key = os.getenv("SPARK_SECRET_KEY") or hashlib.sha256(
    (str(Path(__file__).resolve().parent) + "spark-fallback").encode()
).hexdigest()

# Auto-detect platform: on Windows, tmux runs via WSL; on Linux, directly
_IS_WINDOWS = platform.system() == "Windows"
_TMUX_PREFIX = ["wsl", "tmux"] if _IS_WINDOWS else ["tmux"]


def _tmux_cmd(*args):
    """Build a tmux command list, adding 'wsl' prefix on Windows."""
    return _TMUX_PREFIX + list(args)

# File logger
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

# Terminal base URL — default to localhost ttyd
_TERMINAL_BASE = os.getenv("SPARK_TERMINAL_URL", "http://localhost")

SESSIONS = [
    {"id": "dev", "name": "Dev", "tmux": "claude", "ttyd_port": 7682,
     "terminal_url": f"{_TERMINAL_BASE}:7682"},
    {"id": "alpha", "name": "Alpha", "tmux": "claude2", "ttyd_port": 7683,
     "terminal_url": f"{_TERMINAL_BASE}:7683"},
    {"id": "bravo", "name": "Bravo", "tmux": "claude3", "ttyd_port": 7684,
     "terminal_url": f"{_TERMINAL_BASE}:7684"},
]

# In-memory state
_active_session_id = SESSIONS[0]["id"]
_last_text = None


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


def send_to_claude(text):
    """Send keystrokes to the active tmux session."""
    global _last_text
    _last_text = text
    tmux = get_session()["tmux"]
    cmd = text.strip().lower().rstrip(".")
    key = VOICE_COMMANDS.get(cmd)
    if key:
        result = subprocess.run(
            _tmux_cmd("send-keys", "-t", tmux, *key.split()),
            capture_output=True, timeout=15,
        )
        logging.info(f"KEY cmd='{cmd}' session={tmux} rc={result.returncode}")
    else:
        result = subprocess.run(
            _tmux_cmd("send-keys", "-t", tmux, "-l", "--", text),
            capture_output=True, timeout=15,
        )
        subprocess.run(
            _tmux_cmd("send-keys", "-t", tmux, "Enter"),
            capture_output=True, timeout=15,
        )
        logging.info(f"SEND text='{text[:80]}' session={tmux}")


# --- Main routes ---

@app.route("/")
def home():
    active = get_session()
    resp = make_response(render_template("chat.html",
        session=active, sessions=SESSIONS))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# --- Tmux helpers ---

def _pane_info():
    """Return {tmux_session: {"cmd": ..., "path": ...}} for all sessions."""
    try:
        result = subprocess.run(
            _tmux_cmd("list-panes", "-a", "-F",
             "#{session_name}\t#{pane_current_command}\t#{pane_current_path}"),
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
    return {k: v["cmd"] for k, v in _pane_info().items()}


_SHELLS = {"bash", "sh", "zsh", "fish", "dash"}


# --- API routes ---

@app.route("/api/sessions")
def api_sessions():
    cmds = _pane_commands()
    out = []
    for s in SESSIONS:
        d = dict(s)
        cmd = cmds.get(s["tmux"])
        d["running"] = cmd
        d["alive"] = bool(cmd) and cmd not in _SHELLS
        out.append(d)
    return jsonify({"sessions": out, "active": _active_session_id})


CLAUDE_MODELS = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
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
        model_key = data.get("model", "opus")
        effort = data.get("effort", "medium")
        model_id = CLAUDE_MODELS.get(model_key, CLAUDE_MODELS["opus"])
        if effort not in CLAUDE_EFFORTS:
            effort = "medium"
        cmd = cmd.format(model=model_id, effort=effort)
    for s in SESSIONS:
        if s["id"] == sid:
            tmux = s["tmux"]
            subprocess.run(_tmux_cmd("respawn-pane", "-k", "-t", tmux),
                           capture_output=True, timeout=15)
            time.sleep(0.5)
            work_dir = "/mnt/c/dev"
            subprocess.run(_tmux_cmd("send-keys", "-t", tmux,
                            f"cd {work_dir} && {cmd}", "Enter"),
                           capture_output=True, timeout=15)
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
            s["custom_name"] = True
            logging.info(f"SESSION_RENAME {sid} -> {name}")
            return jsonify({"ok": True, "session": s})
    return jsonify({"error": "Unknown session"}), 400


ALLOWED_KEYS = {
    "Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right",
    "Space", "PageUp", "PageDown", "C-c", "C-z", "C-d",
}


@app.route("/api/key", methods=["POST"])
def key():
    data = request.get_json()
    k = (data.get("key") or "").strip()
    if k not in ALLOWED_KEYS:
        return jsonify({"error": "Key not allowed"}), 400
    tmux = get_session()["tmux"]
    subprocess.run(
        _tmux_cmd("send-keys", "-t", tmux, k),
        capture_output=True, timeout=15,
    )
    return jsonify({"ok": True})


@app.route("/api/scroll", methods=["POST"])
def scroll():
    data = request.get_json()
    direction = data.get("direction", "up")
    tmux = get_session()["tmux"]
    if direction == "up":
        subprocess.run(_tmux_cmd("copy-mode", "-t", tmux), capture_output=True, timeout=15)
        subprocess.run(_tmux_cmd("send-keys", "-t", tmux, "-X", "page-up"), capture_output=True, timeout=15)
    else:
        subprocess.run(_tmux_cmd("copy-mode", "-q", "-t", tmux), capture_output=True, timeout=15)
    return jsonify({"ok": True})


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
    send_to_claude(text)
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
    print(f"[Spark] Voice layer on port {PORT}")
    app.run(host=HOST, port=PORT, debug=True)
