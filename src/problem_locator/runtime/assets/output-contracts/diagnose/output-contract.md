# Methods diagnosis output contract

This is a breaking, Methods-only output protocol. Write exactly one valid,
unambiguous UTF-8 JSON object to `output/method-diagnosis.draft.json`. The Server
validates the schema and atomically normalizes equivalent whitespace and key
ordering before hashing or consuming the draft. A UTF-8 BOM, duplicate object
key, non-finite number, invalid UTF-8, or invalid schema remains an error. Do not write
`output/job_outcome.draft.json`, do not create proposal drafts, and do not run an
outcome sealer.

Logparse has already run in a separate product-owned pass and is unavailable in
this pass. Read only these server-frozen inputs:

- `inputs/request.json`
- `inputs/target_logs.json`
- the `log_path` files listed by `inputs/target_logs.json`
- `inputs/logparse-receipt.json`

The receipt is server-owned. Do not copy its hash into the draft. Diagnose only
with the pinned Methods package in `METHODS_SKILL_FILE` sections and the exact
frozen target-log bytes. Scan every target log for every method's declared
`evidence_markers`; do not stop after the first plausible match.

The top-level object has exactly these fields:

```json
{
  "schema_version": 1,
  "status": "CONFIRMED",
  "confirmed_methods": ["method_id"],
  "candidate_methods": [],
  "evidence": [
    {
      "method_id": "method_id",
      "summary": "bounded evidence-based summary",
      "identity_tokens": ["exact-token-from-cited-lines"],
      "sources": [
        {
          "source_id": "server_source_id",
          "line_number": 1,
          "marker": "declared evidence marker",
          "line": "exact complete frozen log line"
        }
      ]
    }
  ],
  "limitations": [],
  "safety_notes": []
}
```

Allowed `status` values are `CONFIRMED`, `PARTIAL`, and `INSUFFICIENT`.
`confirmed_methods` and `candidate_methods` contain only IDs from `methods.json`
and are disjoint. Every confirmed method has at least one evidence item; evidence
may name only a confirmed method. Each source copies the exact `source_id`,
one-based `line_number`, complete line, and a marker declared by that method.
Every `identity_tokens` value occurs in the cited source lines and the sorted pair
`(method_id, identity_tokens)` is unique.

`CONFIRMED` requires a confirmed method. `INSUFFICIENT` requires empty
`confirmed_methods` and `evidence`. Do not infer an absent marker, invent a log
line, widen a target, or use narrative, filenames, summaries, stdout, stderr, or
prior output as evidence. Finish by atomically publishing only the Methods draft
named above; the Server owns its Canonical JSON encoding.
