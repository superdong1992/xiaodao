from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from problem_locator.contracts import (
    AgentJobOutcomeDraftV2,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.agent_json import InvalidJsonBytesError
from problem_locator.runtime.outcome_finalizer import (
    DRAFT_FINALIZATION_MARKER_RELATIVE_PATH,
    DRAFT_OUTCOME_RELATIVE_PATH,
    SealedAgentOutcomeDraftMarker,
    seal_agent_outcome_draft,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _v2_draft(name: str) -> dict[str, object]:
    value = _fixture(name)
    value.pop("outcome_id", None)
    value.pop("produced_at", None)
    value.pop("decision_audit", None)
    value["schema_version"] = 2
    value["rule_claims"] = []
    if value["job_type"] != "ROUTE" and value["result_type"] != "FAILED":
        value["rule_claims"] = [
            {
                "rule_id": "required_rule",
                "claimed_result": "UNKNOWN",
                "fact_refs": [],
                "citations": [],
                "explanation": "The server must recompute this rule.",
            }
        ]
    return value


def _pretty_reversed(value: object) -> bytes:
    if isinstance(value, dict):
        value = {
            key: json.loads(json.dumps(child))
            for key, child in reversed(list(value.items()))
        }
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "output").mkdir(parents=True)
    (root / "runtime/tool-state").mkdir(parents=True)
    return root


def _pending_requirement(
    *,
    requirement_id: str,
    job_id: str,
    kind: str,
    name: str,
) -> dict[str, object]:
    constraints = (
        {
            "value_type": "STRING",
            "min_utf8_bytes": 1,
            "max_utf8_bytes": 256,
            "pattern": None,
            "allowed_values": [],
        }
        if kind == "INPUT"
        else {
            "allowed_content_types": ["application/zip"],
            "min_count": 1,
            "max_count": 1,
        }
    )
    return {
        "requirement_id": requirement_id,
        "kind": kind,
        "name": name,
        "prompt": f"Provide {name}.",
        "required": True,
        "constraints": constraints,
        "status": "OPEN",
        "requested_by_job_id": job_id,
        "fulfilled_by_refs": [],
        "supplement_policy": "MISSING_ONLY",
    }


def _mixed_wait_draft() -> dict[str, object]:
    value = _fixture("agent-job-outcome-draft-diagnosis.json")
    input_ids = [
        "00000000-0000-0000-0000-000000000096",
        "00000000-0000-0000-0000-000000000097",
    ]
    attachment_id = "00000000-0000-0000-0000-000000000098"
    job_id = str(value["job_id"])
    payload = value["payload"]
    assert isinstance(payload, dict)
    state_delta = payload["state_delta"]
    assert isinstance(state_delta, dict)
    value["result_type"] = "NEED_INPUT"
    payload["candidate_conclusion_draft"] = None
    payload["requested_input"] = input_ids
    payload["requested_attachments"] = [attachment_id]
    state_delta["add_pending_requirements"] = [
        _pending_requirement(
            requirement_id=input_ids[0],
            job_id=job_id,
            kind="INPUT",
            name="caller_service",
        ),
        _pending_requirement(
            requirement_id=input_ids[1],
            job_id=job_id,
            kind="INPUT",
            name="rpc_method",
        ),
        _pending_requirement(
            requirement_id=attachment_id,
            job_id=job_id,
            kind="ATTACHMENT",
            name="log_archive",
        ),
    ]
    return value


def test_sealer_canonicalizes_v2_draft_without_minting_server_fields(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    path = root / DRAFT_OUTCOME_RELATIVE_PATH
    path.write_bytes(_pretty_reversed(_v2_draft("agent-job-outcome-route.json")))

    marker = seal_agent_outcome_draft(root)

    draft_bytes = path.read_bytes()
    draft = parse_canonical_json_bytes(draft_bytes, AgentJobOutcomeDraftV2)
    assert draft.schema_version == 2
    assert "outcome_id" not in json.loads(draft_bytes)
    assert "produced_at" not in json.loads(draft_bytes)
    assert not (root / "output/job_outcome.json").exists()
    assert marker.size == len(draft_bytes)
    assert marker.sha256 == hashlib.sha256(draft_bytes).hexdigest()
    marker_bytes = (root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH).read_bytes()
    assert parse_canonical_json_bytes(
        marker_bytes,
        SealedAgentOutcomeDraftMarker,
    ) == marker


def test_sealer_accepts_need_input_with_multiple_inputs_and_attachment(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    path = root / DRAFT_OUTCOME_RELATIVE_PATH
    path.write_bytes(_pretty_reversed(_mixed_wait_draft()))

    seal_agent_outcome_draft(root)

    draft = parse_canonical_json_bytes(path.read_bytes(), AgentJobOutcomeDraftV2)
    assert draft.result_type.value == "NEED_INPUT"
    assert len(draft.payload.requested_input) == 2
    assert len(draft.payload.requested_attachments) == 1
    assert len(draft.payload.state_delta.add_pending_requirements) == 3
    assert (root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH).is_file()


def test_sealer_rejects_need_attachment_that_also_requests_input(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    path = root / DRAFT_OUTCOME_RELATIVE_PATH
    invalid = _mixed_wait_draft()
    invalid["result_type"] = "NEED_ATTACHMENT"
    path.write_bytes(canonical_json_bytes(invalid))

    with pytest.raises(ValueError, match="forbids requested_input"):
        seal_agent_outcome_draft(root)

    assert not (root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH).exists()


def test_sealer_rejects_agent_owned_final_outcome_path(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / DRAFT_OUTCOME_RELATIVE_PATH).write_bytes(
        canonical_json_bytes(_v2_draft("agent-job-outcome-route.json"))
    )
    (root / "output/job_outcome.json").write_bytes(b"{}\n")

    with pytest.raises(ValueError, match="server-owned"):
        seal_agent_outcome_draft(root)

    assert not (root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH).exists()


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b'\xef\xbb\xbf{"value":1}',
        b'{"value":1,"value":2}',
        b'{"value":NaN}',
        b'{"value":',
        b'\xff',
    ],
)
def test_sealer_rejects_ambiguous_json_without_marker(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    root = _workspace(tmp_path)
    draft_path = root / DRAFT_OUTCOME_RELATIVE_PATH
    draft_path.write_bytes(invalid_bytes)

    with pytest.raises(InvalidJsonBytesError):
        seal_agent_outcome_draft(root)

    assert draft_path.read_bytes() == invalid_bytes
    assert not (root / DRAFT_FINALIZATION_MARKER_RELATIVE_PATH).exists()
