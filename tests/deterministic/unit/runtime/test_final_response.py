from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import pytest

from problem_locator.contracts import StateFile, JobType
from problem_locator.runtime.agent_telemetry import AgentStreamTelemetry
from problem_locator.runtime.final_response import parse_route_response, parse_specialist_response, specialist_prompt, INLINE_INPUT_BYTES, MARKER_INDEX_BYTES
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1, scan_method_markers
from problem_locator.runtime.methods_skill import load_specialized_skill_registration
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


@pytest.mark.parametrize("matched", [False, True])
def test_route_recovers_only_reason_and_preserves_frozen_routing(matched):
    job = route_job()
    ref = job.available_skill_refs[0] if matched else None
    raw = ('{"skill_id":' + json.dumps(ref.id if ref else None)
           + ',"reason":"选择 "foo" 方法","confidence":0.9}')
    result = parse_route_response(raw, job)
    assert result.draft.payload.skill_ref == ref
    assert result.draft.payload.reason == '选择 "foo" 方法'
    assert result.draft.payload.confidence == 0.9
    assert result.route_recovery is not None
    assert result.route_recovery.raw_bytes == raw.encode()


@pytest.mark.parametrize("skill,confidence", [
    ("unknown", "0.9"), (None, "true"), (None, "NaN"), (None, "1.1"),
    (123, "0.9"), (None, '"0.9"'), (None, "-0.1"),
])
def test_route_reason_recovery_never_bypasses_routing_validation(skill, confidence):
    raw = ('{"skill_id":' + json.dumps(skill)
           + ',"reason":"选择 "foo" 方法","confidence":' + confidence + '}')
    with pytest.raises(RuntimeExecutionError):
        parse_route_response(raw, route_job())


def test_route_valid_quotes_do_not_create_recovery_or_bypass_reason_limit():
    value = {"skill_id": None, "reason": '选择 "foo" 方法；路径 C:\\logs\\new', "confidence": 0.9}
    parsed = parse_route_response(json.dumps(value), route_job())
    assert parsed.route_recovery is None
    assert parsed.draft.payload.reason == value["reason"]
    raw = '{"skill_id":null,"reason":"' + 'x' * 1024 + '"foo"' + '","confidence":0.9}'
    with pytest.raises(RuntimeExecutionError):
        parse_route_response(raw, route_job())


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


@pytest.mark.parametrize('prefix,suffix', [
    ('\ufeff', ''), ('```json\n', '\n```'), ('\ufeff```\r\n', '\r\n```'),
])
def test_route_and_specialist_accept_complete_model_presentation_wrappers(prefix, suffix):
    route = '{"skill_id":null,"reason":"无匹配","confidence":1}'
    assert parse_route_response(prefix + route + suffix, route_job()).draft.payload.skill_ref is None
    value = {'schema_version': 1, 'status': 'INSUFFICIENT', 'confirmed_methods': [],
        'candidate_methods': [], 'evidence': [], 'limitations': ['缺少证据'], 'safety_notes': []}
    text = prefix + json.dumps(value, ensure_ascii=False) + suffix
    result = parse_specialist_response(text)
    assert result.draft.status == 'INSUFFICIENT'
    assert result.raw_bytes == text.encode('utf-8')
    assert json.loads(result.canonical_bytes) == value


def test_specialist_preserves_all_items_before_independent_evidence_validation():
    value = {'schema_version': 1, 'status': 'INSUFFICIENT', 'confirmed_methods': [],
        'candidate_methods': [], 'evidence': [{'source_id': 'source_1'}, 'invalid item'],
        'limitations': ['缺少证据'], 'safety_notes': []}
    text = '\ufeff```json\n' + json.dumps(value) + '\n```'
    result = parse_specialist_response(text, preserve_evidence_items=True)
    assert result.draft == value
    assert json.loads(result.canonical_bytes) == value
    assert result.raw_bytes == text.encode('utf-8')
    with pytest.raises(RuntimeExecutionError):
        parse_specialist_response(text)


@pytest.mark.parametrize('prefix,suffix', [
    ('## 分析\n最终结果如下：\n```json\r\n', '\r\n```'),
    ('## 分析\n最终结果如下：\n', ''),
])
def test_route_and_specialist_extract_final_json_with_raw_receipts(prefix, suffix):
    route = '{"skill_id":null,"reason":"无匹配","confidence":1}'
    routed = parse_route_response(prefix + route + suffix, route_job())
    assert routed.draft.payload.skill_ref is None
    assert routed.route_recovery is None
    assert routed.model_json_extraction.raw_bytes == (prefix + route + suffix).encode()
    value = {'schema_version': 1, 'status': 'INSUFFICIENT', 'confirmed_methods': [],
        'candidate_methods': [], 'evidence': [], 'limitations': ['缺少证据'], 'safety_notes': []}
    text = prefix + json.dumps(value, ensure_ascii=False) + suffix
    result = parse_specialist_response(text)
    assert result.draft.status == 'INSUFFICIENT'
    assert result.raw_bytes == text.encode()
    assert result.model_json_extraction.raw_bytes == result.raw_bytes
    assert json.loads(result.model_json_extraction.effective_bytes) == value


def test_markdown_prefix_cannot_hide_private_capability_from_specialist():
    text = '## private-token\n{"note":"plain"}'
    with pytest.raises(RuntimeExecutionError):
        parse_specialist_response(text, secrets=('private-token',), preserve_evidence_items=True)


@pytest.mark.parametrize('text', ['[]', 'null', '```json\n{}\n``` trailing'])
def test_specialist_preserving_items_still_rejects_non_object_and_invalid_json(text):
    with pytest.raises(RuntimeExecutionError):
        parse_specialist_response(text, preserve_evidence_items=True)


@pytest.mark.parametrize('secret_spelling', ['private-token', r'private-\u0074oken'])
def test_specialist_model_wrapper_cannot_hide_private_capabilities(secret_spelling):
    text = '```json\n{"note":"' + secret_spelling + '"}\n```'
    with pytest.raises(RuntimeExecutionError):
        parse_specialist_response(text, secrets=('private-token',), preserve_evidence_items=True)


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


def _marker_fixture(tmp_path, contents=None):
    inputs = tmp_path / 'inputs'
    inputs.mkdir(exist_ok=True)
    for name in ['request.json', 'target_logs.json', 'logparse-receipt.json']:
        (inputs / name).write_bytes(b'{}')
    skill = load_specialized_skill_registration(
        Path(__file__).parents[3] / 'fixtures/components/runtime-catalog/skill-dir/rpc-log-analysis'
    )
    contents = contents if contents is not None else {'client': 'rpc deadline exceeded request_id=42\n'}
    logs = []
    for source, text in contents.items():
        content = text.encode('utf-8')
        logs.append(FrozenTargetLogV1(source, f'inputs/target-logs/{source}.log', sha256(content).hexdigest(), content))
    return skill, tuple(logs)


def _marker_section(prompt):
    start = prompt.index('\nThis complete server-computed literal-marker index ')
    end = prompt.index('\n<<<END SERVER_MARKER_INDEX>>>\n', start) + len('\n<<<END SERVER_MARKER_INDEX>>>\n')
    section = prompt[start:end]
    return section, json.loads(section[section.index('{'):section.index('\n<<<END SERVER_MARKER_INDEX>>>')])


def test_specialist_index_reuses_all_shared_unicode_hits_without_copying_logs(tmp_path, monkeypatch):
    skill, logs = _marker_fixture(tmp_path, {
        'client': 'noise\nSTRASSE request_id=1\nStrasse request_id=2\n',
        'server': 'Straße request_id=3\nONLY_SECOND request_id=3\n',
    })
    first = replace(skill.methods.methods[0], id='first-method', evidence_markers=('Straße',), activation_markers=('Straße',))
    second = replace(first, id='second-method', evidence_markers=('Straße', 'ONLY_SECOND'))
    skill = replace(skill, methods=replace(skill.methods, methods=(first, second)))
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    assert len(receipt.marker_hits) == 7

    def no_rescan(**kwargs):
        raise AssertionError('the prompt must reuse this execution\'s existing scan')

    monkeypatch.setattr('problem_locator.runtime.methods_grounding.scan_method_markers', no_rescan)
    prompt, inlined, size = specialist_prompt('all method cards', tmp_path, logs, skill_load=receipt, skill=skill)
    section, index = _marker_section(prompt)
    assert index == {
        'schema_version': 1, 'complete': True,
        'method_markers': {'first-method': ['Straße'], 'second-method': ['Straße', 'ONLY_SECOND']},
        'source_hits': {'client': {'Straße': [2, 3]}, 'server': {'Straße': [1], 'ONLY_SECOND': [2]}},
    }
    assert inlined and size == len(prompt.encode('utf-8'))
    assert len(section.encode('utf-8')) <= MARKER_INDEX_BYTES
    assert 'Hits are not confirmations.' in section
    for log in logs:
        assert prompt.count(log.content.decode('utf-8')) == 1
        assert 'request_id=' not in section


def test_specialist_empty_index_preserves_every_source_and_full_logs(tmp_path):
    skill, logs = _marker_fixture(tmp_path, {'client': 'no declared marker\n', 'server': 'other context\n'})
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    prompt, inlined, size = specialist_prompt('complete cards', tmp_path, logs, skill_load=receipt, skill=skill)
    assert _marker_section(prompt)[1] == {
        'schema_version': 1, 'complete': True, 'method_markers': {},
        'source_hits': {'client': {}, 'server': {}},
    }
    assert inlined and size == len(prompt.encode('utf-8'))
    assert all(log.content.decode('utf-8') in prompt for log in logs)


@pytest.mark.parametrize('mutation', [
    {'package_tree_sha256': '0' * 64},
    {'scanned_source_ids': ('another-source',)},
    {'loaded_method_ids': ('unknown-method',)},
    {'loaded_method_ids': ('rpc-call-timeout', 'rpc-call-timeout')},
    {'marker_hits': (('unknown-source', 'rpc deadline exceeded', 1),)},
    {'marker_hits': (('client', 'invented marker', 1),)},
    {'marker_hits': (('client', 'rpc deadline exceeded', 0),)},
    {'marker_hits': (('client', 'rpc deadline exceeded', True),)},
    {'marker_hits': (('client', 'rpc deadline exceeded', '1'),)},
    {'marker_hits': (('client',),)},
])
def test_specialist_index_rejects_mismatched_identity_and_invalid_hits(tmp_path, mutation):
    skill, logs = _marker_fixture(tmp_path)
    receipt = replace(scan_method_markers(skill=skill, target_logs=logs), **mutation)
    with pytest.raises(ValueError, match='Specialist marker index'):
        specialist_prompt('cards', tmp_path, logs, skill_load=receipt, skill=skill)


@pytest.mark.parametrize('missing', ['skill', 'skill_load'])
def test_specialist_index_requires_skill_and_receipt_together(tmp_path, missing):
    skill, logs = _marker_fixture(tmp_path)
    kwargs = {'skill': skill, 'skill_load': scan_method_markers(skill=skill, target_logs=logs)}
    del kwargs[missing]
    with pytest.raises(ValueError, match='requires both'):
        specialist_prompt('cards', tmp_path, logs, **kwargs)


def test_specialist_oversized_index_is_omitted_whole_without_dropping_inputs(tmp_path):
    skill, logs = _marker_fixture(tmp_path, {'client': 'rpc deadline exceeded\n' * 4000})
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    original = specialist_prompt('all method cards', tmp_path, logs)
    indexed = specialist_prompt('all method cards', tmp_path, logs, skill_load=receipt, skill=skill)
    assert indexed == original
    assert indexed[1] and '<<<SERVER_MARKER_INDEX>>>' not in indexed[0]
    assert logs[0].content.decode('utf-8') in indexed[0]


def test_specialist_index_includes_fixed_instructions_in_its_byte_limit(tmp_path, monkeypatch):
    skill, logs = _marker_fixture(tmp_path)
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    prompt = specialist_prompt('cards', tmp_path, logs, skill_load=receipt, skill=skill)[0]
    section_bytes = len(_marker_section(prompt)[0].encode('utf-8'))
    monkeypatch.setattr('problem_locator.runtime.final_response.MARKER_INDEX_BYTES', section_bytes)
    assert specialist_prompt('cards', tmp_path, logs, skill_load=receipt, skill=skill)[0] == prompt
    monkeypatch.setattr('problem_locator.runtime.final_response.MARKER_INDEX_BYTES', section_bytes - 1)
    assert specialist_prompt('cards', tmp_path, logs, skill_load=receipt, skill=skill) == specialist_prompt('cards', tmp_path, logs)


def test_specialist_index_never_forces_inline_inputs_into_file_tools(tmp_path):
    skill, logs = _marker_fixture(tmp_path)
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    baseline_size = specialist_prompt('', tmp_path, logs)[2]
    indexed_size = specialist_prompt('', tmp_path, logs, skill_load=receipt, skill=skill)[2]
    exact_context = 'x' * (INLINE_INPUT_BYTES - indexed_size)
    exact = specialist_prompt(exact_context, tmp_path, logs, skill_load=receipt, skill=skill)
    assert exact[1] and exact[2] == INLINE_INPUT_BYTES == len(exact[0].encode('utf-8'))
    assert '<<<SERVER_MARKER_INDEX>>>' in exact[0]
    no_room_context = exact_context + 'x'
    no_room = specialist_prompt(no_room_context, tmp_path, logs, skill_load=receipt, skill=skill)
    assert no_room == specialist_prompt(no_room_context, tmp_path, logs)
    assert no_room[1] and no_room[2] == baseline_size + len(no_room_context)


def test_specialist_large_inputs_keep_index_and_every_original_file_path(tmp_path):
    skill, logs = _marker_fixture(tmp_path, {'client': 'first\n' + 'x' * INLINE_INPUT_BYTES + '\nrpc deadline exceeded\nlast\n'})
    cards = b'first-card\n' + b'y' * INLINE_INPUT_BYTES + b'\nlast-card'
    (tmp_path / 'inputs/methods-package.txt').write_bytes(cards)
    receipt = scan_method_markers(skill=skill, target_logs=logs)
    baseline = specialist_prompt('all method cards', tmp_path, logs)
    prompt, inlined, size = specialist_prompt('all method cards', tmp_path, logs, skill_load=receipt, skill=skill)
    section, index = _marker_section(prompt)
    assert not inlined and size == baseline[2] + len(section.encode('utf-8'))
    assert index['source_hits'] == {'client': {'rpc deadline exceeded': [3]}}
    assert prompt.replace(section, '') == baseline[0]
    for name in ['request.json', 'target_logs.json', 'logparse-receipt.json', 'methods-package.txt', 'client.log']:
        assert name in prompt
    assert (tmp_path / 'inputs/methods-package.txt').read_bytes() == cards
