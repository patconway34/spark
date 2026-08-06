# tmux swallows a trailing `;` in user text

## What breaks

Send `ls -la;` from the phone and the terminal receives `ls -la`. The semicolon
is gone. No error, no warning — the text just arrives slightly wrong.

A semicolon *inside* the text (`echo foo;bar`) is fine. A semicolon on its own
(`;`) is fine. Only a **trailing** one is eaten.

## Why

tmux's argv lexer scans for command separators before the subcommand ever sees
its arguments. A word ending in an unescaped `;` is treated as "end of this
command" and the `;` is stripped. `--` stops *option* parsing, not this — the
separator scan happens earlier.

So `tmux send-keys -t spark1 -l -- "ls -la;"` sends `ls -la`.

This is not a WSL problem. It bit the old per-call `wsl -e tmux` path and the
persistent helper identically, because both hand the same argv to tmux.

## The fix

`_tmux_literal()` in `app.py` escapes a trailing `;` as `\;`, which tmux
unescapes back to a literal semicolon on delivery. It is applied to every place
user text enters a tmux argv:

- `send_to_claude()` — the `send-keys -l` batch
- `/api/type` — raw character typing
- `/api/paste-text` — the `set-buffer` batch

Escape at the **call site**, not inside `tmux_helper.py`. The helper is not the
only path — when it is unavailable Spark falls back to spawning `wsl -e tmux`,
and that path needs the same escaping. Fixing it in one place above both is why
`_tmux_literal` sits next to `_tmux_run`.

## Guard

`test_tmux_paths.py` covers this — trailing semi, double trailing, lone
semicolon, plus unicode and backslashes, across both send paths. Run it after
touching anything in the tmux plumbing:

```
python test_tmux_paths.py
```

It builds a throwaway `spark_selftest` session, so it is safe to run against a
live Spark.
