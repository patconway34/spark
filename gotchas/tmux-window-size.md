# tmux window-size: `latest`, not `largest`

**Symptom (largest):** phone view is cut off on the right — the pane is pinned
at a desktop width (e.g. 192 cols) and the phone can't shrink it.

**Symptom (latest, the accepted trade-off):** a desktop client or a
bogus-sized non-tty client (WSL reports `131072x1 screen size is bogus`) can
briefly resize the pane and force Claude to repaint.

**Decision:** Spark is **phone-first**, so `start.sh` sets:
```
tmux set -g window-size latest
tmux set -g aggressive-resize on
```
The pane follows whichever client is active — the phone wins when it's in use.
`largest` was tried (2026-07-17) to stop resize thrash and immediately broke
the phone (spillover right); reverted same day.

**Rules:**
- Don't switch to `largest`/`smallest` without testing the phone.
- If the phone is cut off on the right: check whether a desktop browser tab is
  holding the terminal open at a wider size — close it and reload the phone.
- Avoid running `wsl tmux attach`-like/interactive commands from pipes; the
  bogus 131072x1 client size is what thrashes panes.
