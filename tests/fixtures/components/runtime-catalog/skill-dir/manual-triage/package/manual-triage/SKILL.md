---
name: manual-triage
description: Diagnose a service incident from frozen structured evidence without Logparse.
---

# Manual service triage

Read frozen `request.json` and the compact `evaluation_input` from runtime context. Use request values for declared inputs. Its `observations` catalog deduplicated physical evidence lines, `markers` catalog declared literals, and ordered `evaluations` contain the `events` available to each method. Log evidence comes only from `evaluation_input`; do not rescan markers or source evidence. Evaluate every `evaluation_ref` in `evaluation_input.evaluations` order and return only `evaluation_ref`, `verdict`, `supporting_event_refs`, and `reason`; use `UNKNOWN` when the evidence cannot decide the method rule.
