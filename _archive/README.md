# _archive

Files Spark no longer uses, kept rather than deleted in case the reasoning is
needed later. Nothing here is imported or launched by anything.

| File | Why it is here |
|---|---|
| `wsl_port_bridge.py/.err/.log` | A WSL→Windows port forwarder from before ttyd bound 0.0.0.0. Nothing references it. |
| `test_tmux_paths.py` | One-off probe for the WSL/Windows tmux path boundary. Its finding is now a comment in `app.py` (`_wsl_to_win`). |
| `_workspaces.json` | Per-tab folder/file/view state from the IDE build (Oct 2-6). Workspaces were removed 2026-10-07; a tab carries only a name now. |
| `spark.log.1` | Rotated log. |
| `_listen_*.wav` | Leftover push-to-talk recording. |

The IDE itself — file tree, viewer, outline, architecture instruments, git
panel, service-link and coupling measurements — was not archived here because
git has it: see tag `v1.0.0` and commit `e7132e5`.
