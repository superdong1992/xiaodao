"""Generic follow-ups preserve immutable reference inputs through execution."""
import json

from tests.deterministic.integration.test_followup_generic_round import (
    generic_round, _seed, _handoff, _get,
)
from tests.deterministic.integration.test_website_agent import website


def test_modified_followup_reference_never_publishes_a_new_report(generic_round):
    s = generic_round
    _seed(s)
    _handoff(s, "另一台服务器为什么写入抖动？")
    original_execute = s.backend.execute

    def mutate(**kwargs):
        result = original_execute(**kwargs)
        path = kwargs["workspace_root"] / "inputs/followup-context.json"
        path.chmod(0o644)
        path.write_text(json.dumps({"original_problem": "篡改的问题"}), encoding="utf-8")
        return result

    s.monkeypatch.setattr(s.backend, "execute", mutate)
    for _ in range(8):
        s.agent.run_once(s.cid)
        assert s.stack.scheduler.wait_until_idle(20)
        if s.store.get_status(s.cid).status == "FAILED":
            break
    child = s.store.get_run(s.cid, s.child)
    assert child["status"] == "FAILED" and not child["report_available"]
    assert s.stack.repository.read_case(s.first["case_id"]).model_dump_json() == s.original_case
    assert _get(s.client, s.prefix + f"?include=report&run_id={s.parent}")["result"] == s.first["result"]
    assert s.preliminary.calls == 0 and len(s.backend.calls) == 2
