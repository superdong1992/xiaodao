# Methods V2 Specialist output contract

Write exactly one UTF-8 JSON array to
`output/method-diagnosis.draft.json`. The array itself is the document root. Do
not wrap it in an object or Markdown and do not write
`output/job_outcome.draft.json`.

The Server has already scanned the logs once. Use only the server-produced
`inputs/request.json` frozen user facts,
`inputs/method-evidence-graph.json`,
`inputs/method-evaluation-plan.json`, and the pinned Methods package. Apply a
request value only when the method rule names its required user input. Log
evidence comes only from the Evidence Graph and Evaluation Plan; do not read or
scan target logs or rebuild evidence references.

Return one item for every Evaluation Plan item, in exact plan order:

```json
[
  {
    "evaluation_ref": "eval-<server value>",
    "verdict": "CONFIRMED",
    "supporting_event_refs": ["event-<server value>"],
    "reason": "short method-rule evaluation"
  }
]
```

Every item has exactly `evaluation_ref`, `verdict`, `supporting_event_refs`, and
`reason`. `evaluation_ref` must equal the corresponding plan value. `verdict` is
exactly one of `CONFIRMED`, `REJECTED`, or `UNKNOWN`. For `CONFIRMED`,
`supporting_event_refs` is a non-empty subset of that plan item's
`evidence_event_refs`, retaining their plan order. For `REJECTED` or `UNKNOWN`,
it is an empty array. Select only exact event refs issued in the Evaluation Plan.
Do not return hit refs and do not copy or invent markers, raw log text, line
numbers, hashes, identity values, or any other evidence fields. `reason` is
non-empty and explains the rule decision without quoting those fields. Do not
omit, add, duplicate, or reorder evaluations.

If the Server invokes this same SPECIALIST role once to repair a structure or
coverage error, fix only the reported JSON shape, field set, plan coverage, plan
order, exact evaluation or event reference, supporting-event relation, verdict
enum, or empty reason. Return the same four fields for every item. Keep the
evaluation meaning unchanged. There is no second repair.
