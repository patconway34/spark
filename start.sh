#!/bin/bash
# Spark — start tmux sessions + ttyd terminals (3 numbered sessions)
#
# Tab N = sparkN on port 7681+N. Models: all opus 5. Three terminals so one
# swipe each way reaches the other two and all three stay loaded (instant switch).
# A NEW tmux session auto-launches claude with its tab's model; existing
# sessions are never touched (running conversations survive restarts).

SESSIONS=("spark1" "spark2" "spark3")
PORTS=(7682 7683 7684)
MODELS=("claude-opus-5" "claude-opus-5" "claude-opus-5")
WORK_DIR="/mnt/c/dev"

# Auth model (2026-07-18): terminals are served same-origin under
# <spark-host>/term/<session> and protected by Cloudflare Access on that
# hostname (plus the Spark token). ttyd carries NO HTTP Basic Auth: a
# same-origin iframe's Basic-Auth challenge is silently blocked by mobile
# Chrome, which rendered the terminals as a blank white page on the phone.
# ttyd binds 0.0.0.0 and is only reachable via the Windows cloudflared tunnel
# (127.0.0.1:<port>), never directly from the network.

echo "=== Spark start ==="
echo "Working directory: $WORK_DIR"

# --- Kill old ttyd instances ---
echo "Killing old ttyd instances..."
pkill -f "ttyd.*-p 76[89]" 2>/dev/null || true  # covers 7682-7696 (all spark ports)
sleep 2  # let ports release — 7682 once failed to bind with only 1s

# --- Create tmux sessions (skip if already running) ---
echo "Setting up tmux sessions..."
for i in "${!SESSIONS[@]}"; do
    s="${SESSIONS[$i]}"
    m="${MODELS[$i]}"
    if tmux has-session -t "$s" 2>/dev/null; then
        echo "  $s — already running"
    else
        tmux new-session -d -s "$s" -c "$WORK_DIR"
        # Fresh session: launch claude with this tab's model
        tmux send-keys -t "$s" "claude --model $m" Enter
        echo "  $s — created (claude --model $m)"
    fi
done

# Size windows to the most recently active client. Spark is phone-first, so the
# pane must follow the phone's (narrower) width — otherwise a desktop client at
# 192 cols pins the pane wide and the phone view spills off the right edge.
# ("largest" was tried for resize-stability, but the real instability was the
#  alt-screen scroll issue, fixed in /api/scroll — so latest is correct here.)
tmux set -g window-size latest 2>/dev/null
tmux set -g aggressive-resize on 2>/dev/null

# --- Launch ttyd for each session (per-terminal themes from theme.json, 1-15) ---
# theme.json holds a base "terminal" theme (text + ANSI) plus a list of
# "terminal_backgrounds" cycled across the 15 terminals, so each gets a different
# faded background shade. Edit theme.json and re-run this script to recolor.
THEME_FILE="/mnt/c/dev/spark/theme.json"
DEFAULT_THEME='{"background":"#fbf1c7","foreground":"#3c3836","cursor":"#3c3836","cursorAccent":"#fbf1c7","selectionBackground":"#d5c4a1","black":"#fbf1c7","red":"#cc241d","green":"#98971a","yellow":"#d79921","blue":"#458588","magenta":"#b16286","cyan":"#689d6a","white":"#7c6f64","brightBlack":"#928374","brightRed":"#9d0006","brightGreen":"#79740e","brightYellow":"#b57614","brightBlue":"#076678","brightMagenta":"#8f3f71","brightCyan":"#427b58","brightWhite":"#3c3836"}'
N=${#SESSIONS[@]}
mapfile -t THEMES < <(/usr/bin/python3 - "$THEME_FILE" "$N" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    n = int(sys.argv[2])
    b = d["terminal"]
    terms = d.get("terminals") or []
    default_bg = b.get("background", "#fbf1c7")
    for i in range(n):
        t = dict(b)
        bg = terms[i % len(terms)].get("background", default_bg) if terms else default_bg
        t["background"] = bg
        t["cursorAccent"] = bg
        print(json.dumps(t))
except Exception:
    pass
PYEOF
)
if [ ${#THEMES[@]} -lt "$N" ]; then
    echo "  (theme.json unreadable — using default Gruvbox Light for all $N)"
    THEMES=(); for _i in $(seq 1 "$N"); do THEMES+=("$DEFAULT_THEME"); done
fi
LABELS=("1" "2" "3")

echo "Launching ttyd terminals..."
for i in "${!SESSIONS[@]}"; do
    s="${SESSIONS[$i]}"
    p="${PORTS[$i]}"
    t="${THEMES[$i]}"
    # Read-only (no -W): all input reaches tmux via the backend (send-keys through
    # /api/key & /api/paste-text), never through ttyd. A writable ttyd auto-focuses
    # its xterm textarea on load, which pops the mobile soft keyboard on every tab
    # switch. Read-only = display-only terminal = the keyboard only ever comes from
    # the "Type here" control.
    setsid ttyd -p "$p" \
        -b "/term/$s" \
        -t scrollback=1000 \
        -t fontSize=14 \
        -t enableClipboard=true \
        -t cursorBlink=true \
        -t cursorStyle=bar \
        -t disableLeaveAlert=true \
        -t "theme=$t" \
        tmux attach -t "$s" > /dev/null 2>&1 &
    echo "  $s — port $p (${LABELS[$i]})"
done

echo ""
echo "=== Ready ==="
echo "Terminals: ports ${PORTS[*]}"
