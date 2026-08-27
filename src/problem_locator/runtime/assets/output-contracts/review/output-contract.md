# Methods review output contract

This is a breaking, Methods-only REVIEW protocol. Write exactly one valid,
unambiguous UTF-8 JSON object to `output/method-review.draft.json`. The Server
validates the schema and atomically normalizes equivalent whitespace and key
ordering before hashing or consuming the draft. A UTF-8 BOM, duplicate object
key, non-finite number, invalid UTF-8, or invalid schema remains an error. Do not write
`output/job_outcome.draft.json`, do not create proposals, and do not run an
outcome sealer. Logparse is unavailable.

Independently review the fixed Candidate and Evidence against:

- `inputs/method-diagnosis.json`, the exact prior grounded Methods diagnosis;
- `inputs/method-grounding-audit.json`, the server verification receipt; and
- the pinned Methods package in the `METHODS_SKILL_FILE` sections.

Do not continue the Specialist's session and do not accept its summary as proof.
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
      "reason": "bounded independent review reason"
    }
  ],
  "limitations": []
}
```

Allowed top-level and finding verdicts are `PASS`, `NEED_MORE_EVIDENCE`, and
`REJECT`. Findings must cover the prior diagnosis's exact set of
`(method_id, identity_tokens)` identities, with no omission, addition, or
duplicate. Preserve every identity token exactly; reordering tokens does not
change identity, but changing their bytes does.

A top-level `PASS` requires every finding to pass. `REJECT` requires at least one
rejected finding. `NEED_MORE_EVIDENCE` requires at least one finding with that
verdict. Finish by atomically publishing only the Methods review draft named
above; the Server owns its Canonical JSON encoding.
