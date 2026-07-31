# Route output contract

Atomically replace `output/job_outcome.json` with a ROUTE `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`. The job binding must match the current instruction exactly. Stdout, stderr, Markdown, and partial files are never business output.
