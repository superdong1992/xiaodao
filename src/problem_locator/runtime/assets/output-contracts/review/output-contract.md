# Review output contract

Atomically replace `output/job_outcome.json` with a REVIEW `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`. PASS is valid only with all four issue arrays empty. Stdout, stderr, Markdown, and partial files are never business output.
