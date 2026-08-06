#!/usr/bin/env python3
"""Persistent tmux command runner — lives inside WSL, talks over stdin/stdout.

WHY THIS EXISTS
    Spark runs on Windows; tmux lives in WSL. Every `wsl -e tmux ...` spawn
    costs ~130ms of pure interop overhead — measured with `wsl -e true`, which
    does nothing and still takes 130ms. tmux itself contributes ~0ms. That cost
    landed on every keystroke, every scroll, and every 10s session poll.

    This process is launched ONCE and kept alive. Spark writes a JSON request
    on stdin, we run the tmux commands natively (no interop), and write one
    JSON line back. Round trip is sub-millisecond.

    A pipe rather than a TCP socket on purpose: no listening port to secure, no
    dependency on WSL2 localhost-forwarding behaviour, and the process dies
    automatically when Spark does.

PROTOCOL  (one JSON object per line, both directions)
    ->  {"id": 7, "cmds": [["send-keys", "-t", "spark1", "-l", "--", "hi"],
                           ["send-keys", "-t", "spark1", "Enter"]]}
    <-  {"id": 7, "results": [{"rc": 0, "out": "", "err": ""}, ...]}

    On a malformed request we still reply (with "error") so the caller is never
    left blocking on a read that will not arrive.

    Commands run in order and ALWAYS all run, even if an earlier one fails —
    the caller inspects per-command rc. `cmds` is a list so a two-step action
    (send text, then Enter) costs one round trip instead of two.

NOTE
    argv is passed to tmux directly via execve — no shell. That preserves
    format strings like #{alternate_on}, which `wsl tmux ...` would mangle
    because bash treats # as a comment (see gotchas/wsl-tmux-format-strings.md).
"""
import json
import subprocess
import sys

TMUX = "/usr/bin/tmux"
# Generous: a wedged tmux server should surface as an error, not a hung Spark.
TIMEOUT = 15


def run_one(argv):
    """Run a single tmux command. Never raises — failures come back as rc."""
    try:
        p = subprocess.run(
            [TMUX] + [str(a) for a in argv],
            capture_output=True, timeout=TIMEOUT,
            encoding="utf-8", errors="replace",
        )
        return {"rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": "", "err": f"timeout after {TIMEOUT}s"}
    except Exception as e:  # missing binary, bad argv, ...
        return {"rc": -1, "out": "", "err": f"{type(e).__name__}: {e}"}


def main():
    # Line-buffered stdout so every reply is flushed the moment it is written;
    # a buffered reply would leave Spark blocking until the pipe filled up.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            cmds = req.get("cmds") or []
            if not isinstance(cmds, list):
                raise ValueError("cmds must be a list")
            resp = {"id": req_id, "results": [run_one(c) for c in cmds]}
        except Exception as e:
            resp = {"id": req_id, "error": f"{type(e).__name__}: {e}",
                    "results": []}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
