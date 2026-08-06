# Spark Gotchas — READ BEFORE BUILDING

Hard-won lessons from breaking Spark. **Any Claude session working on Spark must
read every file in this folder before changing code.** Each file is one gotcha:
what breaks, why, and the known-good fix. Do not "improve" a fix without
understanding the gotcha it guards against.

| File | Gotcha |
|------|--------|
| [scroll-alt-screen.md](scroll-alt-screen.md) | PgUp shows the Claude splash instead of chat history |
| [wsl-tmux-format-strings.md](wsl-tmux-format-strings.md) | `wsl tmux` eats `#{...}` format strings (bash comment) |
| [mobile-iframe-basic-auth.md](mobile-iframe-basic-auth.md) | ttyd basic auth = blank white terminals on mobile |
| [mobile-renderer-memory.md](mobile-renderer-memory.md) | 4 same-origin iframes crash mobile Chrome to white |
| [tmux-window-size.md](tmux-window-size.md) | window-size largest = phone view cut off on the right |
| [api-key-boundary.md](api-key-boundary.md) | ANTHROPIC_API_KEY: Listen button only, never terminals |
| [ttyd-restart.md](ttyd-restart.md) | ttyd restart races and self-killing pkill |
| [tmux-trailing-semicolon.md](tmux-trailing-semicolon.md) | `ls -la;` arrives as `ls -la` — tmux eats a trailing `;` |
| [wsl-interop-cost.md](wsl-interop-cost.md) | Every `wsl -e ...` costs ~130ms — why the tmux helper exists |
| [misc.md](misc.md) | Smaller traps: send-keys truncation, debug beacons, mouse mode |
