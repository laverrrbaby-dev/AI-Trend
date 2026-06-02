from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_TEST_COMMAND = "python3 -m unittest discover tests"
LOG_ROOT = Path(".codex") / "session-log"


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a local checkpoint commit for a Codex session.")
    parser.add_argument("--summary", required=True, help="Short commit summary after the 'checkpoint:' prefix.")
    parser.add_argument("--details", default="", help="Brief description of the session changes.")
    parser.add_argument(
        "--tests",
        default=DEFAULT_TEST_COMMAND,
        help=f"Test command to run before committing. Default: {DEFAULT_TEST_COMMAND!r}",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Restrict the checkpoint to a path changed in this session. Repeat as needed.",
    )
    args = parser.parse_args()

    root = git_root()
    if root is None:
        print("Not inside a Git repository.", file=sys.stderr)
        return 2

    scoped_paths = [normalize_path(root, item) for item in args.path]
    initial_status = git_status(root, scoped_paths)
    if not initial_status.strip():
        scope = "selected paths" if scoped_paths else "worktree"
        print(f"No uncommitted changes in {scope}; checkpoint skipped.")
        return 0

    test_result = run_command(args.tests, cwd=root)
    test_status = "passed" if test_result.returncode == 0 else "failed"

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_ROOT / f"{timestamp}.md"
    log_abs = root / log_path
    log_abs.parent.mkdir(parents=True, exist_ok=True)

    log_abs.write_text(
        build_log(
            summary=args.summary,
            details=args.details,
            tests=args.tests,
            test_status=test_status,
            test_result=test_result,
            status=initial_status,
            diff_stat=diff_stat(root, scoped_paths),
            scoped_paths=scoped_paths,
            log_path=log_path,
        ),
        encoding="utf-8",
    )

    paths_to_add = scoped_paths + [str(log_path)]
    add_changes(root, paths_to_add if scoped_paths else None)

    staged_status = git_status(root, [], staged_only=True)
    if not staged_status.strip():
        print("No staged changes after creating the session log; checkpoint skipped.")
        return 0

    subject = f"checkpoint: {clean_summary(args.summary)}"
    body = "\n".join(
        [
            f"Session log: {log_path}",
            f"Tests: {test_status} ({args.tests})",
            "",
            args.details.strip(),
        ]
    ).strip()
    commit = run_git(root, ["commit", "-m", subject, "-m", body], check=False)
    if commit.returncode != 0:
        print(commit.stdout, end="")
        print(commit.stderr, end="", file=sys.stderr)
        return commit.returncode

    commit_hash = run_git(root, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()
    print(f"Checkpoint commit: {commit_hash}")
    print(f"Session log: {log_path}")
    print(f"Tests: {test_status}")
    if test_result.returncode != 0:
        print("Test command failed; checkpoint was still committed for rollback and traceability.")
    return 0


def git_root() -> Path | None:
    result = run_command("git rev-parse --show-toplevel", cwd=Path.cwd())
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def normalize_path(root: Path, raw_path: str) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        return str(path.resolve().relative_to(root))
    return str(path)


def git_status(root: Path, paths: list[str], staged_only: bool = False) -> str:
    args = ["status", "--porcelain=v1"]
    if staged_only:
        args.append("--untracked-files=no")
    if paths:
        args += ["--", *paths]
    return run_git(root, args, check=True).stdout


def diff_stat(root: Path, paths: list[str]) -> str:
    has_head = run_git(root, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0
    if not has_head:
        return "(initial commit; no HEAD diff stat available)"

    args = ["diff", "--stat", "HEAD", "--"]
    if paths:
        args.extend(paths)
    result = run_git(root, args, check=False)
    if result.returncode == 0:
        return result.stdout.strip() or "(no tracked-file diff stat before staging)"
    return "(diff stat unavailable)"


def add_changes(root: Path, paths: list[str] | None) -> None:
    args = ["add", "-A"]
    if paths:
        args += ["--", *paths]
    run_git(root, args, check=True)


def run_git(root: Path, args: list[str], check: bool) -> CommandResult:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    command_result = CommandResult(result.returncode, result.stdout, result.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return command_result


def run_command(command: str, cwd: Path) -> CommandResult:
    env = os.environ.copy()
    src_path = cwd / "src"
    if src_path.exists():
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src_path) if not existing else f"{src_path}{os.pathsep}{existing}"
    result = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, check=False, env=env)
    return CommandResult(result.returncode, result.stdout, result.stderr)


def clean_summary(summary: str) -> str:
    cleaned = " ".join(summary.strip().split())
    return cleaned[:80] if len(cleaned) > 80 else cleaned


def fenced(text: str) -> str:
    if not text.strip():
        return "(empty)"
    return text.rstrip()


def build_log(
    *,
    summary: str,
    details: str,
    tests: str,
    test_status: str,
    test_result: CommandResult,
    status: str,
    diff_stat: str,
    scoped_paths: list[str],
    log_path: Path,
) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    scope = "\n".join(f"- `{item}`" for item in scoped_paths) if scoped_paths else "- Entire worktree"
    quoted_tests = shlex.quote(tests)
    return f"""# Session Checkpoint

- Timestamp: {now}
- Summary: {summary.strip()}
- Session log: `{log_path}`
- Test status: {test_status}

## Details

{details.strip() or "(no details provided)"}

## Scope

{scope}

## Changed Files Before Checkpoint

```text
{fenced(status)}
```

## Diff Stat Before Checkpoint

```text
{fenced(diff_stat)}
```

## Verification

- Command: `{quoted_tests}`
- Exit code: {test_result.returncode}

### stdout

```text
{fenced(test_result.stdout)}
```

### stderr

```text
{fenced(test_result.stderr)}
```
"""


if __name__ == "__main__":
    raise SystemExit(main())
