from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


async def _probe(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                tools = [_jsonable(tool) for tool in listed.tools]
                names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
                if names != EXPECTED_TOOLS:
                    raise RuntimeError("CLAUDE_DEEPSEEK_PUBLIC_TOOL_SET_INVALID")
                return {
                    "schema_version": 1,
                    "status": "PASS",
                    "transport": "streamable-http",
                    "url": url,
                    "server_version": initialized.serverInfo.version,
                    "tools": tools,
                }


def _write_new(path: Path, value: object) -> None:
    payload = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    arguments = _arguments()
    _write_new(arguments.output, asyncio.run(_probe(arguments.url)))


if __name__ == "__main__":
    main()
