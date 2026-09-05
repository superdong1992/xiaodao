from __future__ import annotations

import json
from pathlib import Path
import pytest

from problem_locator.contracts import StateFile, JobType
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.final_response import parse_route_response, parse_specialist_response, specialist_prompt, INLINE_INPUT_BYTES
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from hashlib import sha256


def route_job():
    state = StateFile.model_validate_json((Path(__file__).parents[3] / 'fixtures/contracts/positive/state.json').read_bytes())
    return next(job for case in state.cases.values() for job in case.jobs.values() if job.job_type is JobType.ROUTE)


def test_route_uses_final_three_fields_and_server_pinned_identity():
    job = route_job()
    ref = job.available_skill_refs[0]
    draft = parse_route_response(json.dumps({'skill_id': ref.id, 'reason': '范围匹配', 'confidence': .9}), job).draft
    assert draft.payload.skill_ref == ref
    assert (draft.job_id, draft.case_id, draft.base_state_revision) == (job.job_id, job.case_id, job.base_state_revision)
    assert parse_route_response('{"skill_id":null,"reason":"无匹配","confidence":1}', job).draft.payload.skill_ref is None


@pytest.mark.parametrize('value', [None, '{}', '```json\n{}\n```', '{"skill_id":"unknown","reason":"x","confidence":1}', '{"skill_id":null,"reason":"x","confidence":true}', '{"skill_id":null,"reason":"x","confidence":NaN}', '{"skill_id":null,"reason":"x","confidence":0,"reason":"y"}'])
def test_invalid_route_response_is_rejected_without_repair(value):
    with pytest.raises(RuntimeExecutionError):
        parse_route_response(value, route_job())


def test_stream_result_is_unique_successful_and_not_in_telemetry():
    stream = AgentStreamTelemetry()
    event = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{"secret_text":"body"}'}).encode() + b'\n'
    for byte in event:
        stream.write(bytes([byte]))
    assert stream.final_result == '{"secret_text":"body"}'
    assert 'secret_text' not in json.dumps(stream.snapshot(diagnosis_mode='SPECIALIZED', backend_status='SUCCESS'))
    stream.write(event)
    assert stream.final_result is None


def test_specialist_json_normalizes_without_a_draft_file():
    value = {'schema_version': 1, 'status': 'INSUFFICIENT', 'confirmed_methods': [], 'candidate_methods': [], 'evidence': [], 'limitations': ['缺少证据'], 'safety_notes': []}
    result = parse_specialist_response(json.dumps(value, indent=2))
    assert result.draft.status == 'INSUFFICIENT'
    assert json.loads(result.canonical_bytes) == value


@pytest.mark.parametrize('size,inlined', [(64, True), (INLINE_INPUT_BYTES + 1, False)])
def test_specialist_inlines_complete_input_or_keeps_every_file(tmp_path, size, inlined):
    (tmp_path / 'inputs').mkdir()
    for name in ['request.json', 'target_logs.json', 'logparse-receipt.json']:
        (tmp_path / 'inputs' / name).write_text('{"unique": "' + name + '"}', encoding='utf-8')
    content = b'first\n' + b'x' * size + b'\nlast'
    logs = [FrozenTargetLogV1('source_1', 'inputs/target-logs/source_1.log', sha256(content).hexdigest(), content)]
    context = 'method card one\nmethod card two\n'
    prompt, actual, total = specialist_prompt(context, tmp_path, logs)
    assert actual is inlined
    assert context in prompt
    for name in ['request.json', 'target_logs.json', 'logparse-receipt.json', 'source_1.log']:
        assert name in prompt
    if inlined:
        assert content.decode() in prompt
        assert total == len(prompt.encode()) <= INLINE_INPUT_BYTES
    else:
        assert total > INLINE_INPUT_BYTES
        assert 'first\n' not in prompt


def test_large_method_package_uses_complete_file_without_context_overflow(tmp_path):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    for name in ['request.json', 'target_logs.json', 'logparse-receipt.json']:
        (inputs / name).write_bytes(b'{}')
    cards = b'first-card\n' + b'x' * (300 * 1024) + b'\nlast-card'
    (inputs / 'methods-package.txt').write_bytes(cards)
    prompt, inlined, size = specialist_prompt('Read the complete frozen method package.', tmp_path, [])
    assert not inlined
    assert size > len(cards)
    assert len(prompt.encode()) < 4096
    assert 'inputs/methods-package.txt' in prompt
    assert (inputs / 'methods-package.txt').read_bytes() == cards
