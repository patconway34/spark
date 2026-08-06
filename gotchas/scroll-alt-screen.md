# PgUp shows the Claude splash / old banner instead of chat history

**Symptom:** Hitting PgUp in Spark shows the "Welcome back Patrick!" splash
logo, the WSL login banner, or stale content (old dates) instead of the
conversation. Happens on every terminal.

**Why:** Claude Code renders in the terminal's **alternate screen buffer**,
which has NO tmux scrollback. The tmux scrollback buffer only contains what was
printed *before* `claude` launched: the shell prompt, the WSL login banner, and
the startup splash. So any scroll that enters **tmux copy-mode** on an
alt-screen pane scrolls that stale pre-launch buffer — splash city.

**Known-good fix (in `/api/scroll`, app.py):**
- Detect the buffer per call: `tmux display-message -p -t <sess> '#{alternate_on}'`
- `alternate_on == 1` (Claude TUI owns the pane): send the **real keystrokes** —
  `send-keys PageUp` for up; a burst of ~15 `send-keys PageDown` for down
  (snaps back to live input; extras are harmless no-ops). Claude scrolls its
  OWN transcript.
- `alternate_on == 0` (inline rendering / plain shell): tmux copy-mode is
  correct — `copy-mode -e` + `send-keys -X page-up` / `page-down`.

**History:** Fixed 2026-07-18. Regressed 2026-07-19 — not because the logic was
rewritten, but because the detection silently broke on Windows: see
[wsl-tmux-format-strings.md](wsl-tmux-format-strings.md). `#{alternate_on}`
was truncated to nothing by the shell, detection returned the status line
instead of "1", and every scroll fell into the copy-mode branch.

**Rule:** never scroll an alt-screen pane with tmux copy-mode. And never trust
the alt detection without testing it **from Windows Python**, not from a WSL
shell — the two quote differently.
