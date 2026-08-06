# Smaller traps

**send-keys -l truncates around ~500 chars.** Long text (voice transcripts!)
must go through `set-buffer` + `paste-buffer` — that's why `/api/paste-text`
exists. Don't "simplify" it back to send-keys -l.

**tmux mouse mode is ON.** Touch/wheel scrolling inside the terminal enters
tmux copy-mode. On an alt-screen pane that shows the stale pre-launch buffer
(see scroll-alt-screen.md). If a pane seems stuck showing old content, check
`#{pane_in_mode}` and send `-X cancel`.

**Scroll throttle in chat.html.** `scrollTerminal()` drops calls within 200ms
— hardware key auto-repeat (8BitDo d-pad ~30ms) would otherwise fling the
scroll. Keep the throttle when touching scroll code.

**bfcache restore = dead page.** `beforeunload` blanks iframes; mobile Chrome
can restore the page from bfcache without re-running scripts. `pageshow` with
`e.persisted` triggers `location.reload()`. Remove that and the phone comes
back to a white page after backgrounding.

**Jinja caches templates; `/` is no-store but Spark must be restarted** for
template/app.py changes to serve. Restart via Radar:
`POST http://localhost:5028/api/restart/5023`.

**Debug leftovers to strip someday:** `[CLIENT]` beacon block + LAYOUT
snapshot in chat.html, and the `/test` color-bands route in app.py. They were
added 2026-07-18/19 chasing the mobile white-page bug. Harmless but noisy in
spark.log.

**spark.log has no rotation.** It grows forever; every page load logs beacons.
Trim or rotate it if it gets big.

**Verify from the right side.** WSL bash quoting ≠ Windows subprocess quoting
(see wsl-tmux-format-strings.md). Anything that worked "when I tested in
bash" must also be tested through Windows Python before calling it fixed.
