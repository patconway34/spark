# `wsl tmux ...` silently eats `#{...}` format strings

**Symptom:** From Windows Python, `subprocess.run(["wsl", "tmux",
"display-message", "-p", "-t", "spark1", "#{alternate_on}"])` returns the
default status line (`[spark1] 0:claude, current pane 0 - (...)`) instead of
`1`. Any tmux command using a `#{...}` format or `-F` string misbehaves the
same way. From a WSL bash shell the identical command works fine — which makes
this maddening to debug.

**Why:** plain `wsl <cmd>` runs the command **through bash**, and in bash `#`
starts a comment. `#{alternate_on}` and everything after it is stripped before
tmux ever sees it. tmux, called with no format argument, prints its default.

**Fix:** use `wsl -e tmux ...` — `-e` execs the binary directly with argv
passed through verbatim, no shell, no comment stripping. `_TMUX_PREFIX` in
app.py is `["wsl", "-e", "tmux"]` for this reason (fixed 2026-07-19). Do not
remove the `-e`.

**Corollaries:**
- This also protected against `$`, `;`, quotes etc. in text sent via
  send-keys — with `-e` they pass verbatim instead of being shell-interpreted.
- Any NEW subprocess that shells into WSL (`wsl <anything>`) must either use
  `-e` or accept bash parsing of its arguments. Test format strings from
  **Windows Python**, never only from a bash shell.
- Running `wsl bash -c '...'` with `#{...}` inside single quotes is safe
  (quoting survives), which is why manual bash tests pass while the server
  fails.
