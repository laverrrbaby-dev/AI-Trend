# Repository Instructions

These instructions apply to the whole repository.

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
