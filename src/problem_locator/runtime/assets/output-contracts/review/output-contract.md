# Methods V2 blind Reviewer output contract

Write exactly one UTF-8 JSON array to `output/method-review.draft.json`. The
array itself is the document root. Do not wrap it in an object or Markdown and
do not write `output/job_outcome.draft.json`.

Blindly and independently evaluate only the server-produced
`inputs/request.json` frozen user facts, the compact `evaluation_input` object
in the required Review Target context section, and the pinned Methods package.
`evaluation_input` is the complete model-visible projection of the authoritative
Evidence Graph and Evaluation Plan. Its `sources` catalog includes every frozen
target, including a source with no matching observation; `observations` contains
only matching physical lines. Apply a request value only when the method
rule names its required user input. Do not read or rescan target logs or load
separate Graph/Plan files. The SPECIALIST response, verdicts, reasons, session,
and workspace are not inputs. Do not use `inputs/method-diagnosis.json` or any
prior Specialist conclusion.

Return one item for every Evaluation Plan item, in exact plan order:

```json
[
  {
    "evaluation_ref": "eval-<server value>",
    "verdict": "CONFIRMED",
    "supporting_event_refs": ["event-<server value>"],
    "reason": "short independent method-rule evaluation"
  }
]
```

Every item has exactly `evaluation_ref`, `verdict`, `supporting_event_refs`, and
`reason`. `evaluation_ref` must equal the corresponding plan value. `verdict` is
exactly one of `CONFIRMED`, `REJECTED`, or `UNKNOWN`. For `CONFIRMED`,
`supporting_event_refs` is a non-empty subset of that evaluation item's event
refs, retaining their evaluation order. For `REJECTED` or `UNKNOWN`,
it is an empty array. Select only exact event refs issued in the Evaluation Plan.
Do not return hit refs and do not copy or invent markers, raw log text, line
numbers, hashes, identity values, or any other evidence fields. `reason` is
non-empty and explains the independent rule decision without quoting those
fields. Do not omit, add, duplicate, or reorder evaluations.

If the Server invokes this same REVIEWER role once to repair a structure or
coverage error, fix only the reported JSON shape, field set, plan coverage, plan
order, exact evaluation or event reference, supporting-event relation, verdict
enum, or empty reason. Return the same four fields for every item. Keep the
evaluation meaning unchanged. There is no second repair.
