#!/usr/bin/env python3
"""Spark's Listen/Text engine — the shared retrieval half.

Given a tmux tab (spark1..spark4), figure out WHICH Claude Code conversation
that tab is showing, then pull the last real turn (Patrick's message + Claude's
reply) straight from Claude Code's own transcript file. This is the clean data
source — no screen scraping, no terminal chrome, no off-window truncation.

How the tab -> file bridge works:
  Claude labels conversations by a random sessionId (a .jsonl file), NOT by
  terminal. There's no field that says "spark2". So we FINGERPRINT: capture the
  tab's visible screen (that part is tab-exact), normalize it down to just
  letters+digits (which erases line-wrapping, box-art, bullets, markdown), then
  vote several windows of it against the recent transcripts. The file that most
  windows land in is that tab's conversation.

Runs INSIDE WSL (tmux + ~/.claude both live here). Stdlib only. Prints one JSON
line to stdout: {ok, tab, file, user, assistant}.

Usage:  python3 listen_retrieve.py <tmux-tab>      e.g. spark2
"""
import json, re, glob, os, subprocess, sys, time

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
RECENT_SECS = 2 * 86400          # only consider transcripts touched in last 2 days
WINDOW = 40                      # fingerprint window size (normalized chars)
STRIDE = 60                      # gap between windows


def norm(s):
    """Reduce to lowercase letters+digits only. Kills wrapping/chrome/markdown."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def capture(tab):
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", tab],
                       capture_output=True, text=True, timeout=10)
    return r.stdout or ""


def pane_cwd(tab):
    r = subprocess.run(["tmux", "display-message", "-p", "-t", tab,
                        "#{pane_current_path}"],
                       capture_output=True, text=True, timeout=10)
    return (r.stdout or "").strip()


def project_dirs(cwd):
    """Claude encodes the cwd as a dir name (every non-alnum char -> '-').
    Return the best-guess dir first, then all recent dirs as fallback."""
    dirs = []
    if cwd:
        enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        cand = os.path.join(PROJECTS, enc)
        if os.path.isdir(cand):
            dirs.append(cand)
    for d in glob.glob(os.path.join(PROJECTS, "*")):
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def msg_text(d):
    m = d.get("message", {})
    if not isinstance(m, dict):
        return ""
    c = m.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_tool_result(d):
    c = d.get("message", {}).get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def load_norm(path):
    """Concatenated normalized user+assistant text of a transcript (for matching)."""
    buf = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") in ("user", "assistant"):
                    buf.append(msg_text(d))
    except Exception:
        return ""
    return norm(" ".join(buf))


def find_file(tab):
    """Fingerprint the tab's screen against recent transcripts. Returns path or None."""
    screen = norm(capture(tab))
    if len(screen) < 60:
        return None
    core = screen[:-WINDOW]                       # drop the tail (status bar)
    windows = [core[i:i + WINDOW] for i in range(0, max(1, len(core) - WINDOW), STRIDE)]
    windows = [w for w in windows if len(w) == WINDOW]
    if not windows:
        return None

    now = time.time()
    votes = {}
    for d in project_dirs(pane_cwd(tab)):
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                if now - os.path.getmtime(f) > RECENT_SECS:
                    continue
            except OSError:
                continue
            txt = load_norm(f)
            if not txt:
                continue
            hits = sum(1 for w in windows if w in txt)
            if hits:
                votes[f] = hits
    if not votes:
        return None
    return max(votes, key=votes.get)


def last_turn(path):
    """Last ANSWERED turn: (user_text, assistant_text). If the newest user
    message has no reply yet, fall back to the previous complete turn."""
    msgs = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return "", ""

    turns = []          # each: {"user": str, "assistant": [str, ...]}
    cur = None
    for d in msgs:
        t = d.get("type")
        if t == "user" and not is_tool_result(d):
            txt = msg_text(d).strip()
            if not txt or txt.startswith(("<", "Caveat", "[Request interrupted")):
                continue
            cur = {"user": txt, "assistant": []}
            turns.append(cur)
        elif t == "assistant" and cur is not None:
            txt = msg_text(d).strip()
            if txt:
                cur["assistant"].append(txt)

    for turn in reversed(turns):
        asst = "\n".join(turn["assistant"]).strip()
        if asst:
            return turn["user"], asst
    if turns:
        return turns[-1]["user"], ""      # newest asked, not yet answered
    return "", ""


def main():
    tab = sys.argv[1] if len(sys.argv) > 1 else ""
    if not tab:
        print(json.dumps({"ok": False, "error": "no tab"}))
        return
    path = find_file(tab)
    if not path:
        print(json.dumps({"ok": False, "error": "no transcript match"}))
        return
    user, assistant = last_turn(path)
    print(json.dumps({
        "ok": bool(assistant),
        "tab": tab,
        "file": os.path.basename(path),
        "user": user,
        "assistant": assistant,
    }))


if __name__ == "__main__":
    main()
