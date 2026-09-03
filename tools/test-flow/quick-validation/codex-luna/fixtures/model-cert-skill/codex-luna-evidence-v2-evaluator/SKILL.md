---
name: codex-luna-evidence-v2-evaluator
description: Evaluate one production Evidence V2 plan as the assigned Specialist or blind Reviewer and write only the fixed role response array.
---

# Codex/Luna Evidence V2 evaluator

Follow the production prompt exactly. Read `inputs/request.json`. Evaluate only
the compact `evaluation_input` embedded in the prompt context and the method
cards provided there. Do not read separate Evidence Graph or Evaluation Plan
files or `runtime/context.txt`; those are not model inputs. Graph and Plan remain
server-owned audit and validation records.

Use the `sources` catalog to distinguish every frozen target, including a source
with no matching observation. Evaluate the ordered `events` under every item in
`evaluations`; resolve their matches through the shared `observations` and
`markers` catalogs.

Write exactly one file named by the prompt:

- Specialist: `output/method-diagnosis.draft.json`
- Reviewer: `output/method-review.draft.json`

The file root is a JSON array in `evaluation_input.evaluations` order. Every item contains
only `evaluation_ref`, `verdict`, `supporting_event_refs`, and `reason`.
For `CONFIRMED`, select a non-empty ordered subset of the current evaluation's
event refs. For `REJECTED` or `UNKNOWN`, use an empty array. Do not create
Evidence, Candidate, Artifact, grounding, partial results, or an Outcome. Do
not read any file outside the current workspace.
