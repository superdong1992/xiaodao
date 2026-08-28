# Methods V2 blind Reviewer output contract

Write exactly one UTF-8 JSON array to `output/method-review.draft.json`. The
array itself is the document root. Do not wrap it in an object or Markdown and
do not write `output/job_outcome.draft.json`.

Blindly and independently evaluate only the server-produced
`inputs/method-evidence-graph.json`,
`inputs/method-evaluation-plan.json`, and the pinned Methods package. The
SPECIALIST response, verdicts, reasons, session, and workspace are not inputs.
Do not use `inputs/method-diagnosis.json` or any prior Specialist conclusion.

Return one item for every Evaluation Plan item, in exact plan order:

```json
[
  {
    "evaluation_ref": "eval-<server value>",
    "verdict": "CONFIRMED",
    "reason": "short independent method-rule evaluation"
  }
]
```

Every item has exactly `evaluation_ref`, `verdict`, and `reason`.
`evaluation_ref` must equal the corresponding plan value. `verdict` is exactly
one of `CONFIRMED`, `REJECTED`, or `UNKNOWN`. `reason` is non-empty and explains
the independent rule decision without quoting markers, raw log text, line
numbers, hashes, or identity values. Do not omit, add, duplicate, or reorder
evaluations.

If the Server invokes this same REVIEWER role once to repair a structure or
coverage error, fix only the reported JSON shape, field set, plan coverage, plan
order, exact reference, verdict enum, or empty reason. Keep the evaluation
meaning unchanged. There is no second repair.
