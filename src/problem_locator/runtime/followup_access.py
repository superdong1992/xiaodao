"""Server-side Read/Grep scope and output formatting for report follow-ups.

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


STRUCTURED_OUTPUT_TOOLS = frozenset({"StructuredOutput", "structuredoutput"})
_FILE_TOOLS = {
    "none": frozenset(),
    "read-only": frozenset({"Read"}),
    "read-search": frozenset({"Read", "Grep"}),
}


def file_tools_for_policy(policy: object) -> frozenset[str] | None:
    """Return explicit file capabilities; unknown policies grant nothing."""
    return _FILE_TOOLS.get(policy) if isinstance(policy, str) else None


def permits_tool(workspace: Path, payload: dict, *, file_access="read-search") -> bool:
    allowed = file_tools_for_policy(file_access)
    if allowed is None or not isinstance(payload, dict):
        return False
    tool = payload.get("tool_name")
    inputs = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(inputs, dict):
        return False
    # CLI-generated output formatting has no file capability. Do not apply path
    # checks to answer content, or match external tools by prefix/case folding.
    if tool in STRUCTURED_OUTPUT_TOOLS:
        return True
    if tool not in allowed:
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


def prepare_settings(argv, environment, workspace: Path, *, file_access="read-search") -> Path:
    """Keep provider configuration, replace inherited tools, hooks and plugins."""
    if file_tools_for_policy(file_access) is None:
        raise ValueError("追问文件权限配置无效。")
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
    command = shlex.join([sys.executable, str(Path(__file__).resolve()),
        str(workspace.resolve()), file_access]) + " || exit 2"
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


def _unique_hook_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("工具调用包含重复字段。")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("工具调用包含无效数值。")


def main():
    allowed = False
    try:
        payload = json.load(sys.stdin, object_pairs_hook=_unique_hook_object, parse_constant=_reject_constant)
        allowed = permits_tool(Path(sys.argv[1]), payload, file_access=sys.argv[2])
    except (OSError, ValueError, IndexError, RecursionError):
        pass
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": "仅允许当前任务授权的 inputs 目录访问和结构化结果提交。"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
