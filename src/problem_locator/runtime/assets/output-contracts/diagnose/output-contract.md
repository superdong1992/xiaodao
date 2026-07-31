# Diagnose output contract

Atomically replace `output/job_outcome.json` with a DIAGNOSE `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`. Put proposal content only below its declared `output/proposals/<proposal_key>/` root. Stdout, stderr, Markdown, and partial files are never business output.
