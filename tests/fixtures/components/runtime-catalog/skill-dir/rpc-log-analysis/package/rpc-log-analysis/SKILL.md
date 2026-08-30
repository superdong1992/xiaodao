---
name: rpc-log-analysis
description: Diagnose RPC timeout evidence from one frozen Logparse target set.
---

# RPC timeout log analysis

Read `request.json`, `method-evidence-graph.json`, and `method-evaluation-plan.json`. Use request values for declared inputs. Log evidence comes only from the Evidence Graph and Evaluation Plan; do not rescan logs. Evaluate every `evaluation_ref` in plan order and return only `evaluation_ref`, `verdict`, `supporting_event_refs`, and `reason`; use `UNKNOWN` when the evidence cannot decide the method rule.
