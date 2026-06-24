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
pkill -f "ttyd.*-p 768" 2>/dev/null || true
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

# --- Launch ttyd for each session (distinct colors) ---
THEMES=(
    '{"background":"#d4edda","foreground":"#155724","cursor":"#155724","selectionBackground":"#a3d9a5"}'
    '{"background":"#f8d7da","foreground":"#721c24","cursor":"#721c24","selectionBackground":"#f1a9b0"}'
    '{"background":"#cce5ff","foreground":"#004085","cursor":"#004085","selectionBackground":"#9ec5fe"}'
)
LABELS=("GREEN" "RED" "BLUE")

echo "Launching ttyd terminals..."
for i in "${!SESSIONS[@]}"; do
    s="${SESSIONS[$i]}"
    p="${PORTS[$i]}"
    t="${THEMES[$i]}"
    setsid ttyd -W -p "$p" \
        -t scrollback=100000 \
        -t fontSize=14 \
        -t enableClipboard=true \
        -t cursorBlink=true \
        -t cursorStyle=bar \
        -t "theme=$t" \
        tmux attach -t "$s" > /dev/null 2>&1 &
    echo "  $s — port $p (${LABELS[$i]})"
done

echo ""
echo "=== Ready ==="
echo "Terminals: ports ${PORTS[*]}"
