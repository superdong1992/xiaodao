# Methods V1 diagnosis output contract

Write exactly one canonical UTF-8 JSON object to
`output/method-diagnosis.draft.json`. Do not write
`output/job_outcome.draft.json` or any other Agent Outcome, create
proposal drafts, generate a report, or run an outcome sealer.

Read only `inputs/request.json`, `inputs/target_logs.json`, the `log_path` files
listed there, `inputs/logparse-receipt.json`, and the pinned Methods package.
Scan every target log for every method's declared `evidence_markers`.

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
      "summary": "specific evidence-based finding",
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

`status` is exactly `CONFIRMED`, `PARTIAL`, or `INSUFFICIENT`.
`confirmed_methods` and `candidate_methods` contain only IDs from `methods.json`
and are disjoint. Every confirmed method has evidence. Evidence may name only a
confirmed method. Each source copies the exact source ID, one-based line number,
complete raw line, and a marker declared by that method. Every identity token
must occur in the cited source lines, and each sorted `(method_id,
identity_tokens)` pair must be unique.

`CONFIRMED` requires a confirmed method. `INSUFFICIENT` requires empty
`confirmed_methods` and `evidence`. Never infer an absent marker, invent a line,
widen a target, or use narrative text as evidence. Publish only the canonical
Methods draft named above. The Server rechecks every method, marker, line,
source, identity, and hash before it creates Candidate, Outcome, JSON, or ZIP.
