---
name: codex-luna-evidence-v2-evaluator
description: Evaluate one production Evidence V2 plan as the assigned Specialist or blind Reviewer and write only the fixed role response array.
---

# Codex/Luna Evidence V2 evaluator

Follow the production prompt exactly. Read `inputs/request.json`,
`inputs/method-evidence-graph.json`, `inputs/method-evaluation-plan.json`, and
only the method cards named by that context.

Write exactly one file named by the prompt:

- Specialist: `output/method-diagnosis.draft.json`
- Reviewer: `output/method-review.draft.json`

The file root is a JSON array in Evaluation Plan order. Every item contains
only `evaluation_ref`, `verdict`, and `reason`. Do not create Evidence,
Candidate, Artifact, grounding, partial results, or an Outcome. Do not read any
file outside the current workspace.
