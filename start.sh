#!/bin/bash
# Spark — start tmux sessions + ttyd terminals (3 sessions)
#
# Environment variables (all optional):
#   SPARK_PROJECT_DIR  — working directory for tmux sessions (default: $HOME)

SESSIONS=("claude" "claude2" "claude3")
PORTS=(7682 7683 7684)
WORK_DIR="/mnt/c/dev"

echo "=== Spark start ==="
echo "Working directory: $WORK_DIR"

# --- Kill old ttyd instances ---
echo "Killing old ttyd instances..."
pkill -f "ttyd -W -p 768" 2>/dev/null || true
sleep 1

# --- Create tmux sessions (skip if already running) ---
echo "Setting up tmux sessions..."
for s in "${SESSIONS[@]}"; do
    if tmux has-session -t "$s" 2>/dev/null; then
        echo "  $s — already running"
    else
        tmux new-session -d -s "$s" -c "$WORK_DIR"
        echo "  $s — created"
    fi
done

# --- Launch ttyd for each session ---
echo "Launching ttyd terminals..."
for i in "${!SESSIONS[@]}"; do
    s="${SESSIONS[$i]}"
    p="${PORTS[$i]}"
    nohup ttyd -W -p "$p" \
        -t scrollback=100000 \
        -t fontSize=14 \
        -t enableClipboard=true \
        -t cursorBlink=true \
        -t cursorStyle=bar \
        -t 'theme={"background":"#1e1e1e","cursor":"#ffffff"}' \
        tmux attach -t "$s" > /dev/null 2>&1 &
    echo "  $s — port $p"
done

echo ""
echo "=== Ready ==="
echo "Terminals: ports ${PORTS[*]}"
