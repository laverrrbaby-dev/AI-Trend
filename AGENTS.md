# Repository Instructions

These instructions apply to the whole repository.

## Runtime deployment and synchronization

- The canonical source directory is this Git worktree. The live macOS service runs from the deployed copy at `~/Library/Application Support/TrendRadar`; do not assume that changing files here changes the page currently served at `trend.test`.
- Do not replace the deployed copy with a symlink to this worktree. The worktree is under macOS's user Documents area, and the deployed copy exists in Application Support specifically to avoid launchd/Streamlit access restrictions. Keep the copy-based deployment model.
- After changing application code, tests that affect runtime behavior, runtime scripts, configuration, or UI assets, run the source-to-runtime deployment before claiming the change is available:

  ```bash
  ./scripts/update_local_runtime.sh
  ```

- After deployment, verify both locations before reporting completion:

  ```bash
  test "$(cat "$HOME/Library/Application Support/TrendRadar/config/local/update-source-root.txt")" = "$(pwd)"
  cmp app.py "$HOME/Library/Application Support/TrendRadar/app.py"
  diff -qr src "$HOME/Library/Application Support/TrendRadar/src"
  ```

- Runtime-generated data is not source code and must not be copied blindly from the worktree over the live cache. For refresh/recovery tasks, use the actual runtime root explicitly, or synchronize only a verified artifact after backing up the live data. In particular, crypto alert results must be checked under `~/Library/Application Support/TrendRadar/data/crypto/alerts/`, because that is what the deployed service reads.
- If a user reports that the page differs from the worktree, inspect the running process working directory and the live runtime data path before changing logic. The prior failure mode was restoring `data/crypto/alerts` in the worktree while the service continued reading the separate Application Support copy.

## Checkpoint Commits

- Before modifying files, run `git status --short --branch` and note whether the worktree is already dirty.
- After any Codex session that changes code, tests, docs, scripts, or project configuration, run:

  ```bash
  python3 scripts/checkpoint_commit.py --summary "<short summary>" --details "<what changed>"
  ```

- The checkpoint script runs the default test command, writes `.codex/session-log/<timestamp>.md`, stages the relevant changes, and creates a local `checkpoint: ...` commit.
- If the user explicitly says not to commit or not to checkpoint, skip the checkpoint and say so in the final response.
- If the worktree was dirty before the session, do not include unrelated pre-existing user changes. Either ask for permission to include them, or pass one or more `--path <path>` arguments for only the files changed in the current session. The script always includes its generated session log.
- Report the commit hash, test status, and session log path in the final response.
- Do not push automatically unless the user explicitly asks.
