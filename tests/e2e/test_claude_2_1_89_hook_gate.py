from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "e2e" / "fixtures" / "claude_2_1_89_hook_probe.mjs"
HOOKS = (
    ROOT
    / ".claude"
    / "skills"
    / "problem-locator-client"
    / "references"
    / "client-hooks-settings.json"
)
RELEASE_REQUIRED = "PROBLEM_LOCATOR_RELEASE_GATES_REQUIRED"
EXPECTED_VERSION = "2.1.89 (Claude Code)"


def _npm_cli() -> tuple[Path, Path] | None:
    node = shutil.which("node.exe")
    npm_root = Path(os.environ.get("APPDATA", "")) / "npm"
    cli = npm_root / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    package = cli.with_name("package.json")
    if not node or not cli.is_file() or not package.is_file():
        return None
    metadata = json.loads(package.read_text(encoding="utf-8"))
    if metadata.get("name") != "@anthropic-ai/claude-code":
        return None
    return Path(node), cli


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_json(path: Path, process: subprocess.Popen[str]) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        assert process.poll() is None, "probe server exited before becoming ready"
        time.sleep(0.05)
    raise AssertionError("probe server did not become ready")


@pytest.mark.skipif(os.name != "nt", reason="the compatibility Hook is Windows-only")
def test_official_npm_claude_2_1_89_repairs_model_string_before_mcp(
    tmp_path: Path,
) -> None:
    discovered = _npm_cli()
    if discovered is None:
        if os.environ.get(RELEASE_REQUIRED) == "1":
            pytest.fail("official npm Claude Code 2.1.89 is required")
        pytest.skip("official npm Claude Code is unavailable")
    node, cli = discovered
    version = subprocess.run(
        [os.fspath(node), os.fspath(cli), "--version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert version.returncode == 0
    assert version.stdout.strip() == EXPECTED_VERSION

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    server = subprocess.Popen(
        [os.fspath(node), os.fspath(FIXTURE), os.fspath(evidence)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = _wait_for_json(evidence / "servers-ready.json", server)
        api_port = int(ready["api"])
        mcp_port = int(ready["mcp"])
        request_id = str(ready["request_id"])

        settings = json.loads(HOOKS.read_text(encoding="utf-8"))
        project_text = os.fspath(ROOT).replace("\\", "/")
        for groups in settings["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = hook["command"].replace(
                        "${CLAUDE_PROJECT_DIR}", project_text
                    )
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps(settings, ensure_ascii=False), encoding="utf-8"
        )
        mcp_path = tmp_path / "mcp.json"
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "problem-locator": {
                            "type": "http",
                            "url": f"http://127.0.0.1:{mcp_port}/mcp",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        config_dir = tmp_path / "claude-config"
        config_dir.mkdir()
        dfx_log = evidence / "client-dfx.jsonl"
        environment = dict(os.environ)
        environment.update(
            {
                "ANTHROPIC_AUTH_TOKEN": "hook-probe-token",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api_port}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CONFIG_DIR": os.fspath(config_dir),
                "PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE": os.fspath(dfx_log),
            }
        )
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment.pop(name, None)
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["no_proxy"] = environment["NO_PROXY"]

        claude = subprocess.run(
            [
                os.fspath(node),
                os.fspath(cli),
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                "--mcp-config",
                os.fspath(mcp_path),
                "--strict-mcp-config",
                "--settings",
                os.fspath(settings_path),
                "--setting-sources",
                "user",
                "Call the Problem Locator create_case tool exactly once.",
            ],
            cwd=tmp_path,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=90,
        )
        assert claude.returncode == 0, claude.stderr[-4000:]

        hook_events = _json_lines(dfx_log)
        started = [
            event
            for event in hook_events
            if event.get("event") == "client.hook.tool.started"
            and event.get("operation_id") == request_id
        ]
        assert started
        assert started[-1]["argument_json_types"]["problem_spec"] == "string"
        assert started[-1]["argument_json_types"]["initial_user_facts"] == "string"

        mcp_events = _json_lines(evidence / "mcp-requests.jsonl")
        calls = [
            event["body"]
            for event in mcp_events
            if isinstance(event.get("body"), dict)
            and event["body"].get("method") == "tools/call"
            and event["body"].get("params", {})
            .get("arguments", {})
            .get("request_id")
            == request_id
        ]
        assert calls
        arguments = calls[-1]["params"]["arguments"]
        assert isinstance(arguments["problem_spec"], dict)
        assert isinstance(arguments["initial_user_facts"], list)
        assert arguments["problem_spec"]["statement"] == "连接失败"
        assert arguments["initial_user_facts"] == [{"name": "主机", "value": "节点一"}]

        bypass = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "problem_locator_create_case",
                "arguments": {
                    "request_id": "bypass-string",
                    "problem_spec": '{"statement":"still a string"}',
                    "initial_user_facts": [],
                },
            },
        }
        request = Request(
            f"http://127.0.0.1:{mcp_port}/mcp",
            data=json.dumps(bypass).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            rejected = json.loads(response.read().decode("utf-8"))
        structured = rejected["result"]["structuredContent"]
        assert structured["ok"] is False
        assert structured["error"]["code"] == "VALIDATION_ERROR"
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
