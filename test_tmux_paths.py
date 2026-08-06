"""Functional test for Spark's tmux send paths.

Run:  python test_tmux_paths.py        (from Windows; drives WSL tmux)

Creates a throwaway tmux session, pushes tricky text through both batches that
app.py uses — send-keys and set-buffer/paste-buffer — reads the pane back, and
cleans up. Never touches spark1-3, so it is safe to run against a live Spark.

Guards two things:
  1. The persistent helper (tmux_helper.py) round-trips argv and UTF-8 intact.
  2. _tmux_literal() escaping. tmux's lexer strips an unescaped trailing ';'
     off a word and treats it as a command separator, so "ls -la;" used to
     arrive as "ls -la". Keep tmux_literal() below in step with app.py's copy.
"""
import json
import subprocess
import sys

SESSION = "spark_selftest"
HELPER = ["wsl", "-e", "/usr/bin/python3", "/mnt/c/dev/spark/tmux_helper.py"]

CASES = [
    ("plain ascii",      "hello world"),
    ("semicolon inside", "echo foo;bar"),
    ("trailing semi",    "ls -la;"),
    ("double trailing",  "foo;;"),
    ("lone semicolon",   ";"),
    ("quotes + dollar",  "x=\"$HOME\" 'quoted'"),
    ("unicode",          "caf\u00e9 \u2014 \u4f60\u597d \u2713"),
    ("backslash",        "C:\\dev\\spark"),
    ("long (600 chars)", "A" * 600),
]


def tmux_literal(text):
    """Mirror of app.py's _tmux_literal — keep the two in step."""
    return text[:-1] + "\\;" if text.endswith(";") else text


def main():
    proc = subprocess.Popen(
        HELPER, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="replace", bufsize=1)
    seq = [0]

    def call(*cmds):
        seq[0] += 1
        proc.stdin.write(
            json.dumps({"id": seq[0], "cmds": [list(c) for c in cmds]}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())["results"]

    fails = 0
    run = 0
    for mode in ("send-keys", "paste-buffer"):
        print(f"--- {mode} path ---")
        for label, text in CASES:
            # send-keys -l truncates around 500 chars by design (see
            # gotchas/misc.md) — that is why the paste-buffer path exists.
            if mode == "send-keys" and len(text) > 500:
                print(f"  [skip] {label:18} (send-keys -l truncates >500 by design)")
                continue

            call(["kill-session", "-t", SESSION])
            # `cat` echoes whatever we type straight back into the pane.
            call(["new-session", "-d", "-s", SESSION, "-x", "800", "-y", "50", "cat"])

            esc = tmux_literal(text)
            if mode == "send-keys":
                call(["send-keys", "-t", SESSION, "-l", "--", esc],
                     ["send-keys", "-t", SESSION, "Enter"])
            else:
                call(["set-buffer", "--", esc],
                     ["paste-buffer", "-t", SESSION],
                     ["send-keys", "-t", SESSION, "Enter"])

            pane = call(["capture-pane", "-t", SESSION, "-p"])[0]["out"]
            # Ignore wrapping: a long line comes back split across rows.
            ok = text.replace("\n", "") in pane.replace("\n", "")
            run += 1
            fails += 0 if ok else 1
            shown = text if len(text) <= 30 else f"{text[:27]}...({len(text)})"
            print(f"  [{'OK ' if ok else 'FAIL'}] {label:18} {shown!r}")
            if not ok:
                print(f"         pane tail: {pane.strip().splitlines()[-2:]!r}")

    call(["kill-session", "-t", SESSION])
    proc.stdin.close()
    proc.terminate()

    print()
    print(f"{run - fails}/{run} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
