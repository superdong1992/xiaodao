---
name: rpc-log-analysis
description: Diagnose RPC timeout evidence from one frozen Logparse target set.
---

# RPC timeout log analysis

Read frozen `request.json`, `target_logs.json`, every listed target log, and `logparse-receipt.json`. Scan every method marker. Return only `schema_version`, `status`, `confirmed_methods`, `candidate_methods`, `evidence`, `limitations`, and `safety_notes`. Each confirmed method includes a specific summary, `identity_tokens`, and exact sources with `source_id`, one-based `line_number`, declared marker, and complete log line. Use `PARTIAL` or `INSUFFICIENT` when the evidence is incomplete. The Server owns Candidate, Outcome, USER_RESULT, and ZIP generation.
