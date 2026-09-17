"""Public MCP/REST journeys over production ASGI, storage, dispatch and runtime.

The model and Logparse executables are deterministic fixtures. This proves the
wire contract and delivery lifecycle, not a real user's Agent/Skill execution.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import uuid
import zipfile

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from problem_locator.contracts import ReviewPolicy
from tests.deterministic.journey.test_rpc_timeout import (
    ARCHIVE, PARAMETER_GROUP_A, RPC_CLIENT_LOG, RPC_SERVER_LOG,
    _Stack, _windows_extended_path,
)


ROOT = Path(__file__).resolve().parents[3]
BASE_URL = "http://127.0.0.1:18080"
PROBLEM_FIELDS = (
    "statement", "expected_behavior", "actual_behavior", "scope", "goals",
    "non_goals", "constraints", "completion_criteria",
)


def skill_input(operation, **values):
    """Execute the published examples, so Skill argument drift breaks the gate."""
    skill = (ROOT / ".claude/skills/problem-locator-client/SKILL.md").read_text(encoding="utf-8")
    section = skill.split(f"`problem_locator_{operation}`:", 1)[1]
    template = json.loads(section.split("```json", 1)[1].split("```", 1)[0])
    template.update(values)
    return template


class PublicClient:
    def __init__(self, http, session):
        self.http = http
        self.session = session

    async def call(self, operation, *, error=None, **values):
        arguments = skill_input(operation, **values)
        if self.session is not None:
            result = await self.session.call_tool(f"problem_locator_{operation}", arguments)
            envelope = result.structuredContent
            assert isinstance(envelope, dict), result
            # Application errors are valid MCP envelopes, not protocol errors.
            assert result.isError is False, result
        else:
            body = dict(arguments)
            case_id = body.pop("case_id", None)
            if operation == "create_case":
                body["problem_spec"] = {key: body.pop(key) for key in PROBLEM_FIELDS}
                body["initial_user_facts"] = [
                    {"name": name, "value": value} for name, value in zip(
                        body.pop("initial_user_fact_names"), body.pop("initial_user_fact_values"), strict=True)
                ]
                method, url = "POST", "/api/v1/cases"
            elif operation == "get_case":
                body.pop("include_details")  # REST always returns the full Case view.
                body = {key: value for key, value in body.items() if value is not None}
                method, url = "GET", f"/api/v1/cases/{case_id}"
            elif operation == "prepare_attachment":
                method, url = "POST", f"/api/v1/cases/{case_id}/attachments"
            elif operation == "submit_supplement":
                body["inputs"] = [{"name": name, "value": value} for name, value in zip(
                    body.pop("input_names"), body.pop("input_values"), strict=True)]
                method, url = "POST", f"/api/v1/cases/{case_id}/supplements"
            elif operation == "list_artifacts":
                method, url = "GET", f"/api/v1/cases/{case_id}/artifacts"
            else:
                raise AssertionError(f"未支持的 REST 操作：{operation}")
            response = await self.http.request(method, url, **({"params": body} if method == "GET" else {"json": body}))
            assert response.status_code == (409 if error else 200), response.text
            envelope = response.json()
        assert envelope["ok"] == (error is None), envelope
        if error:
            assert envelope["error"]["code"] == error, envelope
            return envelope["error"]
        assert envelope["error"] is None, envelope
        return envelope["data"]

    async def view(self, case_id):
        return (await self.call("get_case", case_id=case_id, include_details=True))["case_view"]


@asynccontextmanager
async def public_client(stack, protocol):
    app = stack.http_app
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
            if protocol == "rest":
                yield PublicClient(http, None)
            else:
                async with streamable_http_client(BASE_URL + "/mcp", http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        assert len(listed.tools) == 7
                        yield PublicClient(http, session)


@pytest.fixture
def system_stack(tmp_path):
    root = tmp_path.parent / ("pi-" + uuid.uuid4().hex[:8])
    root.mkdir()
    if os.name == "nt":
        root = _windows_extended_path(root)
    release = root / "release"
    release.write_text("pass\n", encoding="utf-8")
    stacks = []

    def build():
        stack = _Stack(root / "data", logparse_record=root / "logparse.json",
            agent_record=root / "agent.jsonl", review_entered=root / "entered",
            review_release=release, seed=f"public-{len(stacks)}")
        stacks.append(stack)
        return stack

    stack = build()
    try:
        yield stack, build, release
    finally:
        for item in reversed(stacks):
            if not getattr(item, "_public_test_closed", False):
                item.shutdown()


def close(stack):
    stack.shutdown()
    stack._public_test_closed = True


async def diagnosis(system_stack, protocol, review):
    stack, build, release = system_stack
    stack.catalog._specialized_review_policy = ReviewPolicy.INDEPENDENT if review else ReviewPolicy.NONE
    raw = "A payment service call to inventory times out."
    create = dict(request_id="public-create", raw_problem_text=raw, statement=raw, actual_behavior=raw)
    async with public_client(stack, protocol) as client:
        created = await client.call("create_case", **create)
        case_id = created["business_receipt"]["case_id"]
        replayed = await client.call("create_case", **create)
        assert replayed["business_receipt"] == created["business_receipt"]
        await client.call("create_case", **{**create, "raw_problem_text": "Different problem"}, error="IDEMPOTENCY_CONFLICT")
        stack.start()
        await asyncio.to_thread(stack.wait_idle)
        waiting = await client.view(case_id)
        assert waiting["status"] == "WAITING_INPUT"
        assert waiting["raw_problem_text"] == raw
        assert waiting["user_facts"] == []
        assert [item["name"] for item in waiting["pending_requirements"] if item["status"] == "OPEN"] == [*PARAMETER_GROUP_A, "log_archive"]

        # A stale write must neither advance the revision nor adopt any input.
        await client.call("submit_supplement", request_id="stale", case_id=case_id,
            expected_case_revision=1, input_names=["problem_time"],
            input_values=[PARAMETER_GROUP_A["problem_time"]], attachment_ids=[], wait_seconds=0,
            error="REVISION_CONFLICT")
        assert await client.view(case_id) == waiting
        for index, (names, values) in enumerate((
            (list(PARAMETER_GROUP_A)[:2], list(PARAMETER_GROUP_A.values())[:2]),
            (list(PARAMETER_GROUP_A)[2:], list(PARAMETER_GROUP_A.values())[2:]),
        )):
            supplied = await client.call("submit_supplement", request_id=f"inputs-{index}", case_id=case_id,
                expected_case_revision=waiting["case_revision"], input_names=names,
                input_values=values, attachment_ids=[], wait_seconds=0)
            assert supplied["business_receipt"]["job_id"] is None
            waiting = await client.view(case_id)
        assert waiting["status"] == "WAITING_ATTACHMENT"

        archive = ARCHIVE.read_bytes()
        prepared = await client.call("prepare_attachment", request_id="prepare", case_id=case_id,
            expected_case_revision=waiting["case_revision"], name="logs.zip", content_type="application/zip",
            declared_size=len(archive), declared_sha256=hashlib.sha256(archive).hexdigest())
        upload = prepared["upload"]
        headers = upload["required_headers"]
        # Same-length corrupt bytes must not become READY; the correct retry must work.
        corrupted = await client.http.put(upload["url"], headers=headers, content=b"x" * len(archive))
        assert corrupted.status_code == 422, corrupted.text
        assert corrupted.json()["error"]["code"] == "RESOURCE_HASH_MISMATCH"
        uploaded = await client.http.put(upload["url"], headers=headers, content=archive)
        assert uploaded.status_code == 200, uploaded.text
        ready = uploaded.json()["data"]
        assert ready["status"] == "READY"
        replay_upload = await client.http.put(upload["url"], headers=headers, content=archive)
        assert replay_upload.status_code == 200, replay_upload.text
        assert replay_upload.json()["data"] == ready
        arguments = dict(request_id="diagnose", case_id=case_id, expected_case_revision=ready["case_revision"],
            input_names=[], input_values=[], attachment_ids=[upload["attachment_id"]], wait_seconds=0)
        submitted = await client.call("submit_supplement", **arguments)
        assert submitted["business_receipt"]["job_id"]
        assert (await client.call("submit_supplement", **arguments))["business_receipt"] == submitted["business_receipt"]
        await asyncio.to_thread(stack.wait_idle)
        resolved = await client.view(case_id)
        assert resolved["status"] == "RESOLVED", resolved
        assert resolved["final_result"]["status"] == "ACCEPTED"
        assert resolved["archive_status"] == "READY"
        listed = (await client.call("list_artifacts", case_id=case_id))["artifacts"]
        assert {item["kind"] for item in listed} == {"USER_RESULT", "USER_RESULT_ARCHIVE"}
        downloads = {}
        for artifact in listed:
            response = await client.http.get(artifact["download_url"])
            assert response.status_code == 200, response.text
            assert int(response.headers["content-length"]) == artifact["size"] == len(response.content)
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
            downloads[artifact["kind"]] = response.content
        report = json.loads(downloads["USER_RESULT"])
        assert report["format_id"] == "problem-locator-diagnosis-v3"
        with zipfile.ZipFile(io.BytesIO(downloads["USER_RESULT_ARCHIVE"])) as bundle:
            assert bundle.testzip() is None
            assert bundle.namelist() == ["result.txt", "archive-manifest.json",
                "client__COMPACT__slot_1__checkout-client-101.log",
                "server__COMPACT__slot_2__inventory-server-202.log"]
            assert bundle.read(bundle.namelist()[2]) == RPC_CLIENT_LOG.encode("utf-8")
            assert bundle.read(bundle.namelist()[3]) == RPC_SERVER_LOG.encode("utf-8")
            assert "1. 定位结论" in bundle.read("result.txt").decode("utf-8")
        jobs = stack.repository.read_case(case_id).jobs.values()
        assert sum(job.job_type.value == "REVIEW" for job in jobs) == int(review)

    close(stack)
    restarted = build()
    restarted.start()
    await asyncio.to_thread(restarted.wait_idle)
    async with public_client(restarted, protocol) as client:
        assert await client.view(case_id) == resolved
        assert (await client.call("list_artifacts", case_id=case_id))["artifacts"] == listed
        for artifact in listed:
            response = await client.http.get(artifact["download_url"])
            assert response.status_code == 200
            assert response.content == downloads[artifact["kind"]]


@pytest.mark.parametrize("review", [False, True], ids=["specialist", "reviewed"])
def test_mcp_diagnosis(system_stack, review):
    asyncio.run(diagnosis(system_stack, "mcp", review))


@pytest.mark.parametrize("review", [False, True], ids=["specialist", "reviewed"])
def test_rest_diagnosis(system_stack, review):
    asyncio.run(diagnosis(system_stack, "rest", review))
