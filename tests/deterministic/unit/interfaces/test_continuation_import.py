"""Continuation rows remain owned, drained and readable during offline import."""
import json

import pytest

from problem_locator.entrypoints import postgres_import as migration
from tests.deterministic.unit.agent.test_report_continuation import create, system


def test_import_accepts_confirmable_draft_without_starting_work(system):
    s = system
    draft = create(s, effective_problem_text="更正后的完整问题")
    with s.repository.database_read() as db:
        tables, _ = migration._source_schema(db)
        before = db.execute("SELECT * FROM agent_continuation_drafts").fetchall()
        migration._validate_drained(db, tables)
        migration._validate_continuations(db, tables)
        assert db.execute("SELECT * FROM agent_continuation_drafts").fetchall() == before
    assert s.service.get(s.cid, draft.draft_id).status == "DRAFT"


def test_import_rejects_unfinished_draft_preparation(system):
    s = system
    create(s, text="时间需要更正")
    with s.repository.database_read() as db:
        tables, _ = migration._source_schema(db)
        with pytest.raises(migration.PostgresImportError) as failed:
            migration._validate_drained(db, tables)
    assert failed.value.code == "SOURCE_NOT_DRAINED"


@pytest.mark.parametrize("damage", ["parent", "report"])
def test_import_rejects_draft_with_wrong_parent_or_report_bytes(system, damage):
    s = system
    draft = create(s)
    with s.repository.database_transaction() as db:
        raw = json.loads(db.execute("SELECT body FROM agent_continuation_drafts").fetchone()[0])
        if damage == "parent":
            raw["parent_run_id"] = "00000000-0000-0000-0000-000000000099"
        else:
            raw["_parent_markdown"] += "changed"
        db.execute("UPDATE agent_continuation_drafts SET parent_run_id=?,body=? WHERE draft_id=?",
            (raw["parent_run_id"], json.dumps(raw), draft.draft_id))
        tables, _ = migration._source_schema(db)
        with pytest.raises(migration.PostgresImportError):
            migration._validate_continuations(db, tables)
