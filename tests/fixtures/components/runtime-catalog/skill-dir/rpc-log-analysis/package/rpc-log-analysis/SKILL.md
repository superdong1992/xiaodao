---
name: rpc-log-analysis
description: Diagnose RPC timeout evidence from one frozen Logparse target set.
---

# RPC timeout log analysis

Read frozen `request.json` and the compact `evaluation_input` from runtime context. Use request values for declared inputs. Its `observations` catalog deduplicated physical log lines, `markers` catalog declared literals, and ordered `evaluations` contain the `events` available to each method. Log evidence comes only from `evaluation_input`; do not rescan markers or target logs. Evaluate every `evaluation_ref` in `evaluation_input.evaluations` order and return only `evaluation_ref`, `verdict`, `supporting_event_refs`, and `reason`; use `UNKNOWN` when the evidence cannot decide the method rule.
