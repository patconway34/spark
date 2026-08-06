# Every `wsl -e ...` costs ~130ms — don't spawn one per action

## The measurement

```
wsl -e true          (does nothing at all)   ->  134 / 127 / 129 ms
wsl -e tmux list-panes -a -F '#{session_name}' ->  132 / 125 / 132 ms
```

Identical. The ~130ms is **Windows->WSL process launch**, not tmux. tmux itself
costs roughly nothing. Anything that shells into WSL pays this toll, every time.

## Why it mattered

Spark runs on Windows; tmux lives in WSL. Originally every tmux touch was its
own `wsl -e tmux ...` spawn, so the toll landed on:

| Action | spawns | delay |
|---|---|---|
| Key press | 1 | ~130 ms |
| Send text (text + Enter) | 2 | ~260 ms |
| Scroll, alt-screen | 2 | ~260 ms |
| Scroll up, normal screen | 3 | ~390 ms |
| Session poll (every 10s) | 1 | ~130 ms |

All of that before the phone's round trip through Cloudflare. It read as
"Spark is a little slow" rather than anything obviously broken, which is
exactly why it survived so long.

## The fix

`tmux_helper.py` runs **once** inside WSL and stays alive. `app.py` talks to it
over stdin/stdout with one JSON object per line. Round trip: **~2ms**. A batch
of commands costs one round trip instead of N, so text+Enter is ~3.6ms.

Measured after: `/api/sessions` went from ~130-150ms to ~5ms.

Design notes worth keeping:

- **A pipe, not a TCP socket.** No listening port to secure, no dependency on
  WSL2 localhost-forwarding behaviour (which differs between NAT and mirrored
  networking), and the helper exits on its own when Spark does — its stdin hits
  EOF and the read loop ends.
- **Always falls back.** Any pipe or protocol trouble drops `_tmux_run` back to
  the original `wsl -e tmux` spawn. Worst case is the old speed, never a dead
  terminal. There is a 30s cooldown so a genuinely broken helper is not
  re-spawned on every request.
- **Self-heals.** If the helper is killed, the next request notices the dead
  process and respawns it — costs one ~100ms request, then back to ~2ms.
- **Responses are id-matched.** A reply whose id does not match the request
  means the stream is desynced and every later read would be off by one, so the
  helper is restarted rather than trusted.

## Don't

- Don't "simplify" `_tmux_run` back to a direct `subprocess.run(_tmux_cmd(...))`.
  That is the slow path, kept only as the fallback.
- Don't move `_tmux_literal()` escaping into the helper — the fallback path
  needs it too. See [tmux-trailing-semicolon.md](tmux-trailing-semicolon.md).
- Don't batch commands whose arguments contain user text with tmux's own `;`
  separator. Batch through the helper's `cmds` list instead, which has no
  parsing at all.

## Still paying the toll

`_PY_PREFIX` calls (`listen_retrieve.py` and friends) still spawn per call.
They are rare and already slow for other reasons, so they were left alone. If
one of them ever lands in a hot path, route it through the helper too.
