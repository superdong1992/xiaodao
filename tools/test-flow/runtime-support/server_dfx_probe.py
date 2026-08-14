from __future__ import annotations

"""Exercise the authoritative Linux MCP DFX contract without client-side DFX."""

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
EXPECTED_VALIDATION_FIELDS = {
    "actual_behavior",
    "completion_criteria",
    "constraints",
    "expected_behavior",
    "goals",
    "non_goals",
    "problem_spec",
    "raw_problem_text",
    "scope",
    "statement",
}
SCALAR_TYPES = {"boolean", "integer", "number", "string"}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _nullable_scalar(schema: object) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("type") in SCALAR_TYPES:
        return True
    variants = schema.get("anyOf")
    if not isinstance(variants, list) or len(variants) < 2:
        return False
    types = {
        item.get("type")
        for item in variants
        if isinstance(item, dict)
    }
    return "null" in types and types - {"null"} <= SCALAR_TYPES


def _flat_schema(schema: object) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if "$defs" in schema or "$ref" in schema:
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    pending: list[object] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if "$defs" in value or "$ref" in value:
                return False
            pending.extend(value.values())
    for property_schema in properties.values():
        if _nullable_scalar(property_schema):
            continue
        if not isinstance(property_schema, dict):
            return False
        if property_schema.get("type") != "array" or not _nullable_scalar(
            property_schema.get("items")
        ):
            return False
    return True


def _validation_fields(result: object) -> list[str]:
    structured = getattr(result, "structuredContent", None)
    if not isinstance(structured, dict) or structured.get("ok") is not False:
        raise RuntimeError("VALIDATION_PROBE_ENVELOPE")
    error = structured.get("error")
    if not isinstance(error, dict) or error.get("code") != "VALIDATION_ERROR":
        raise RuntimeError("VALIDATION_PROBE_CODE")
    details = error.get("details")
    if not isinstance(details, list):
        raise RuntimeError("VALIDATION_PROBE_DETAILS")
    fields = sorted(
        {
            str(detail["field"])
            for detail in details
            if isinstance(detail, dict) and "field" in detail
        }
    )
    if set(fields) != EXPECTED_VALIDATION_FIELDS:
        raise RuntimeError("VALIDATION_PROBE_FIELDS")
    return fields


def _leaf_error_messages(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        return [
            message
            for nested in error.exceptions
            for message in _leaf_error_messages(nested)
        ]
    message = str(error).strip()
    return [message or type(error).__name__]


async def _run(request_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
        async with streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                if names != EXPECTED_TOOLS or not all(
                    _flat_schema(tool.inputSchema) for tool in listed.tools
                ):
                    raise RuntimeError("PUBLIC_TOOL_CONTRACT")
                result = await session.call_tool(
                    "problem_locator_create_case",
                    {
                        "request_id": request_id,
                        "problem_spec": {"statement": "removed composite field"},
                    },
                )
                fields = _validation_fields(result)
                return {
                    "schema_version": 2,
                    "status": "PASS",
                    "client": "official-python-sdk",
                    "server_version": initialized.serverInfo.version,
                    "tool_count": len(names),
                    "tool_names": names,
                    "flat_schema": True,
                    "validation_probe_request_id": request_id,
                    "validation_fields": fields,
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
    if not arguments.request_id or len(arguments.request_id) > 128:
        raise RuntimeError("VALIDATION_PROBE_REQUEST_ID")
    _write_new(arguments.output, asyncio.run(_run(arguments.request_id)))


try:
    main()
except Exception as error:
    leaves = sorted(set(_leaf_error_messages(error)))
    raise SystemExit(f"SERVER_DFX_PROBE_FAILED:{'|'.join(leaves)}") from None
