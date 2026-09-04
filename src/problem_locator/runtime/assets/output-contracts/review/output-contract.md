# Methods V1 independent Review output contract

Write exactly one canonical UTF-8 JSON object to
`output/method-review.draft.json`. Do not write
`output/job_outcome.draft.json` or any other Agent Outcome, create
proposals, generate a report, or run an outcome sealer. Logparse is unavailable.

Independently review `inputs/method-diagnosis.json`,
`inputs/method-grounding-audit.json`, the fixed Candidate and Evidence, and the
pinned Methods package. Do not continue the Specialist session or treat its
summary as proof.

The top-level object has exactly these fields:

```json
{
  "schema_version": 1,
  "verdict": "PASS",
  "findings": [
    {
      "method_id": "method_id",
      "identity_tokens": ["exact-token-from-prior-diagnosis"],
      "verdict": "PASS",
      "reason": "specific independent review reason"
    }
  ],
  "limitations": []
}
```

Top-level and finding verdicts are exactly `PASS`, `NEED_MORE_EVIDENCE`, or
`REJECT`. Findings must cover the prior diagnosis's exact set of `(method_id,
identity_tokens)` identities, without omission, addition, or duplicate. Preserve
every identity token exactly; reordering tokens does not change identity.

A top-level `PASS` requires every finding to pass. `REJECT` requires at least
one rejected finding. `NEED_MORE_EVIDENCE` requires at least one finding with
that verdict. Publish only the canonical Methods review draft named above. The
Server owns final verification and publication.
