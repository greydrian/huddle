"""PostToolUse hook: lint an edited Python file with ruff and feed any
problems back to Claude (exit 2 = stderr shown to the model; the edit has
already happened, so this never blocks). Silently does nothing when ruff
isn't available, so the shared project config can't break other machines."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

payload = json.load(sys.stdin)
file_path = (payload.get("tool_input") or {}).get("file_path") or ""
if not file_path.endswith(".py"):
    sys.exit(0)

path = Path(file_path)
# Find the repo root (worktrees included) to pick up its venv + pyproject.toml.
root = next((p for p in [path.parent, *path.parents] if (p / "pyproject.toml").exists()), None)
if root is None:
    sys.exit(0)

venv_python = Path(__file__).resolve().parents[2] / "venv" / "Scripts" / "python.exe"
if venv_python.exists():
    cmd = [str(venv_python), "-m", "ruff"]
elif shutil.which("ruff"):
    cmd = ["ruff"]
else:
    sys.exit(0)

problems = []
result = subprocess.run(  # noqa: S603 (our own ruff, on the edited path)
    [*cmd, "check", "--output-format", "concise", "--quiet", str(path)],
    cwd=root,
    capture_output=True,
    text=True,
)
if result.returncode == 1 and result.stdout.strip():
    problems.append(f"ruff found issues in {path.name} (fix before committing):\n{result.stdout}")
# CI runs `ruff format --check`, so an unformatted file fails there.
formatted = subprocess.run(  # noqa: S603
    [*cmd, "format", "--check", "--quiet", str(path)], cwd=root, capture_output=True, text=True
)
if formatted.returncode == 1:
    problems.append(f"{path.name} is not ruff-formatted: run `ruff format {path}` (never format by hand)")
if problems:
    print("\n".join(problems), file=sys.stderr)
    sys.exit(2)
sys.exit(0)
