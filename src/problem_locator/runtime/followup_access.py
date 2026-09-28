"""Server-side Read/Grep scope for report follow-ups only.

The same file is Claude's PreToolUse hook. A failed hook command exits with
Claude's blocking code 2, so a broken guard cannot grant a read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat
import sys


def permits_tool(workspace: Path, payload: dict) -> bool:
    tool = payload.get("tool_name")
    inputs = payload.get("tool_input", {})
    if tool not in {"Read", "Grep"} or not isinstance(inputs, dict):
        return False
    value = inputs.get("file_path" if tool == "Read" else "path")
    if not isinstance(value, str) or not value:
        return False
    root = workspace.resolve() / "inputs"
    candidate = Path(os.path.abspath(workspace / value))
    try:
        relative = candidate.relative_to(root)
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current /= part
            metadata = current.lstat()
            if current.is_symlink() or not (stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1):
                return False
        return candidate.is_file() if tool == "Read" else candidate.exists()
    except (OSError, ValueError):
        return False


def prepare_settings(argv, environment, workspace: Path) -> Path:
    """Keep provider configuration, replace inherited tools, hooks and plugins."""
    source = None
    for index, token in enumerate(argv):
        if token == "--settings" and index + 1 < len(argv):
            source = argv[index + 1]
        elif token.startswith("--settings="):
            source = token.split("=", 1)[1]
    if source is None:
        config = Path(environment.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
        source = str(config) if config.is_file() else "{}"
    original = json.loads(source if source.lstrip().startswith("{") else Path(source).read_text(encoding="utf-8"))
    provider_env = {key: value for key, value in original.get("env", {}).items()
        if key.startswith("ANTHROPIC_") or key in {"API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}}
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), str(workspace.resolve())]) + " || exit 2"
    policy = {"env": provider_env,
        "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]}}
    if "model" in original:
        policy["model"] = original["model"]
    target = workspace / "runtime" / "followup-settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        target.chmod(0o600)
        json.dump(policy, stream, ensure_ascii=False)
    return target


def main():
    allowed = False
    try:
        payload = json.load(sys.stdin)
        allowed = isinstance(payload, dict) and permits_tool(Path(sys.argv[1]), payload)
    except (OSError, ValueError, IndexError):
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": "报告追问只允许读取和搜索 inputs 目录。"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
