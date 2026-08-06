# ANTHROPIC_API_KEY boundary: Listen button ONLY, never the terminals

**The rule:** Spark's Claude Code terminals ALWAYS run on the Max
subscription. The **Listen button (🎧) is the only API surface** — it's
Patrick's deliberate API-practice path for FDE work.

**How it's wired (keep it this way):**
- `notify.py` `listen` mode → `summarize_last_response()` → imports
  `anthropic`, calls `load_dotenv("C:/dev/.env")` **inside the function** to
  get `ANTHROPIC_API_KEY`. This is the only place the key is loaded.
- `text` / `play` / `speak` modes go through `mente` = subscription.
- `C:/dev/.env` DOES contain `ANTHROPIC_API_KEY` (added 2026-07-18 for this).
  That is an exception to the workspace-wide "no API key in any .env" rule —
  scoped to notify.py's listen path.

**Never do:**
- Export `ANTHROPIC_API_KEY` into the WSL shell, `.bashrc`, or the tmux
  sessions — the `claude` CLI would silently start billing the API instead of
  the subscription. Verify with:
  `tr '\0' '\n' < /proc/<claude-pid>/environ | grep ANTHROPIC` → must be empty.
- Have app.py itself load `C:/dev/.env` (it loads only `spark/.env`).
- Route terminal traffic (send_to_claude etc.) through anything API-billed.

**Symptom of a breach:** unexpected Anthropic API charges, or `claude` showing
API-billing instead of the Max plan.
