---
name: rpc-log-analysis
description: Diagnose RPC timeout evidence from one frozen Logparse target set.
---

# RPC timeout log analysis

Read `method-evidence-graph.json` and `method-evaluation-plan.json`. Do not rescan logs. Evaluate every `evaluation_ref` in plan order and return only `verdict` and `reason`; use `UNKNOWN` when the evidence cannot decide the method rule.
