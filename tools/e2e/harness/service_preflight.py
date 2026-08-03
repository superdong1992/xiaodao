from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = [
    "problem_locator_create_case",
    "problem_locator_prepare_attachment",
    "problem_locator_submit_supplement",
    "problem_locator_get_case",
    "problem_locator_resume_case",
    "problem_locator_cancel_case",
    "problem_locator_list_artifacts",
]
EXPECTED_CHECKS = [
    "CONFIG",
    "INSTANCE_LOCK",
    "STATE",
    "DATA_DIRECTORIES",
    "RECOVERY",
]


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


async def wait_http(client: httpx.AsyncClient) -> tuple[dict, dict, int]:
    deadline = time.monotonic() + 60.0
    probes = 0
    while time.monotonic() < deadline:
        probes += 1
        try:
            live_response = await client.get("http://127.0.0.1:8000/live")
            ready_response = await client.get("http://127.0.0.1:8000/ready")
            if live_response.status_code == 200 and ready_response.status_code == 200:
                return live_response.json(), ready_response.json(), probes
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError("SERVICE_READINESS_TIMEOUT")


async def main_async() -> dict[str, object]:
    async with httpx.AsyncClient(trust_env=False, timeout=5.0) as http_client:
        live, ready, probes = await wait_http(http_client)
        require(live == {"ok": True, "data": {"status": "live"}, "error": None}, "LIVE_BODY")
        require(ready.get("ok") is True and ready.get("error") is None, "READY_ENVELOPE")
        report = ready.get("data")
        require(isinstance(report, dict) and report.get("ready") is True, "READY_BODY")
        checks = report.get("checks")
        require(isinstance(checks, list), "READY_CHECKS_TYPE")
        require([check.get("name") for check in checks] == EXPECTED_CHECKS, "READY_CHECK_NAMES")
        require(all(check.get("passed") is True and check.get("message") is None for check in checks), "READY_CHECK_VALUES")
        require(report.get("error") is None, "READY_REPORT_ERROR")
        async with streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
    names = [tool.name for tool in listed.tools]
    require(names == EXPECTED_TOOLS, "MCP_TOOL_INVENTORY")
    require(len(set(names)) == 7, "MCP_TOOL_UNIQUENESS")
    require(all(tool.inputSchema.get("type") == "object" for tool in listed.tools), "MCP_INPUT_SCHEMA")
    require(all(tool.outputSchema is not None and tool.outputSchema.get("type") == "object" for tool in listed.tools), "MCP_OUTPUT_SCHEMA")
    return {
        "live": True,
        "mcp_client": "official-python-sdk",
        "mcp_initialize": "pass",
        "mcp_list_tools": "pass",
        "readiness_check_names": EXPECTED_CHECKS,
        "readiness_probes": probes,
        "ready": True,
        "schema_version": 1,
        "tool_count": 7,
        "tool_names": names,
        "tool_schemas_object": True,
    }


def main() -> None:
    payload = asyncio.run(main_async())
    output = Path("/evidence/service-preflight.json")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        stream.write("\n")


try:
    main()
except Exception:
    raise SystemExit("SERVICE_PREFLIGHT_FAILED") from None
