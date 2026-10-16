"""Release successful workspace copies only after all durable consumers finish."""
from __future__ import annotations

from .paths import workspace_owner_id


class CompletedWorkspacePolicy:
    def __init__(self, repository, followups):
        self.repository, self.followups = repository, followups

    def __call__(self, workspace_id):
        owner = workspace_owner_id(workspace_id)
        if self.followups.store.workspace_in_use(owner):
            return False
        with self.repository.database_read() as db:
            task = db.execute("SELECT status FROM agent_followup_tasks WHERE workspace_id=?",
                              (workspace_id,)).fetchone()
            if task is not None:
                return task[0] == "COMPLETED"
            row = db.execute("SELECT case_id FROM object_index WHERE object_id=?", (owner,)).fetchone()
            snapshot = db.execute("SELECT status FROM agent_followup_snapshots WHERE source_job_id=?",
                                  (owner,)).fetchone()
            archive_busy = row is not None and db.execute("SELECT 1 FROM archive_tasks WHERE case_id=? "
                "AND status IN ('PENDING','RUNNING')", (row[0],)).fetchone() is not None
        if row is None:
            return False
        case_id = row[0]
        # This policy is rechecked under the publication barrier. Never acquire
        # a worker's activity mutex here: archive publication takes it first.
        if archive_busy:
            return False
        state = self.repository.read_snapshot(case_id)
        aggregate = state.cases.get(case_id)
        if aggregate is None:
            return False
        job = aggregate.jobs.get(owner)
        case = aggregate.case
        if (job is None or job.status != "SUCCEEDED" or case.active_job_id is not None
                or case.status not in {"RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED"}
                or case.archive_status not in {"READY", "NOT_REQUIRED"}):
            return False
        # With follow-ups enabled, an absent row can still be awaiting observation.
        # Keep that input until the snapshot worker has durably decided its fate.
        return not self.followups.enabled or (
            snapshot is not None and snapshot[0] in {"READY", "EXPIRED"})
