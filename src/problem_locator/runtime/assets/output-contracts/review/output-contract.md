# Methods V1 independent Review output contract

Write exactly one canonical UTF-8 JSON object to
`output/method-review.draft.json`. Do not write
`output/job_outcome.draft.json` or any other Agent Outcome, create
proposals, generate a report, or run an outcome sealer. Logparse is unavailable.

Independently review `inputs/method-diagnosis.json`,
`inputs/method-grounding-audit.json`, the fixed Candidate and Evidence, and the
pinned Methods package. Do not continue the Specialist session or treat its
summary as proof.

请按下面的结构返回审核结果：

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

以上是建议的输出格式。服务端忽略额外字段，未提供的说明和数组使用空值或普通说明，重复条目会合并；无法识别的判断按 `NEED_MORE_EVIDENCE` 处理。strict 模式按本次诊断保留的发现对齐审核，缺项或冲突按证据不足处理，并从逐项审核推导整份结果的判断；advisory 模式采用整份结果的审核判断。这些格式差异不记录异常，不重新调用模型。
