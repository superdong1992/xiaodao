# Methods V1 diagnosis output contract

在最终响应中直接返回一个 UTF-8 JSON 对象，不要使用 Markdown 代码块，不要调用 Write 或创建草稿。服务端负责规范化、核验和报告生成。

完整使用上下文中的方法卡和冻结输入。服务端内联输入时，无需再读取文件；未内联时，只读取 `inputs/request.json`、`inputs/target_logs.json` 及其 `log_path` 文件、`inputs/logparse-receipt.json` 和明确列出的方法卡文件，不能静默裁剪或遗漏。
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

Before submitting, check each source separately: `marker` must be copied exactly
from the current method's `evidence_markers`, and
`marker.casefold() in line.casefold()` must hold for the complete frozen line at
that source ID and line number. Matching ignores case; the submitted marker and
raw line each retain their original spelling. A match in another line or method
is insufficient. Do not skip intervening fields, join fragments, or use regex matching.

For example, the client template `Rpc call SNO %u timeout` yields `Rpc call SNO`,
while the server template `Rpc call %s:%s SNO %u proc timeout` yields `Rpc call`.
In `Rpc call Inventory:Reserve SNO 42 proc timeout`, `Rpc call SNO` is not a
contiguous substring. Use `Rpc call` only if the current method declares it.
The same canonical extraction applies to `{service}:{api}` placeholders. This
example describes citation mechanics, not an additional RPC diagnosis rule.

If a required log line has no matching marker declared by the method, record
the gap in `limitations` and use `PARTIAL` or `INSUFFICIENT` as appropriate.
Never invent, shorten, or rewrite a marker, or omit required evidence while
still confirming the method. A matching marker only grounds the citation; all
Wiki confirmation conditions must also be satisfied.

`CONFIRMED` requires a confirmed method. `INSUFFICIENT` requires empty
`confirmed_methods` and `evidence`. Never infer an absent marker, invent a line,
widen a target, or use narrative text as evidence. Return only the diagnosis JSON.
The Server rechecks every method, marker, line,
source, identity, and hash before it creates Candidate, Outcome, JSON, or ZIP.
