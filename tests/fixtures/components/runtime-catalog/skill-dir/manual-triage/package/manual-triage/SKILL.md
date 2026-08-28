---
name: manual-triage
description: Diagnose a service incident from frozen structured evidence without Logparse.
---

# Manual service triage

Read `method-evidence-graph.json` and `method-evaluation-plan.json`. Do not rescan evidence. Evaluate every `evaluation_ref` in plan order and return only `verdict` and `reason`; use `UNKNOWN` when the evidence cannot decide the method rule.
