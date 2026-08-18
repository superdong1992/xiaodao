---
name: render-problem-locator-trace
description: Render and explain one Problem Locator server-side diagnosis journey from DFX_LOG_DIR by canonical Case ID. Use when reconstructing an end-to-end diagnosis, summarizing current Case progress, explaining why a Case failed or is waiting, or inspecting its deterministic detailed.log and brief.log. Do not use to create, resume, or modify Cases.
---

# Render Problem Locator Trace

Use the product renderer as the only source of human log formatting. Do not parse
`journey.jsonl` in the agent, generate replacement summaries, or fall back to
`debug.jsonl`.

## Workflow

1. Obtain the exact canonical lowercase UUID for the Case. Never guess `latest`,
   choose a Case by directory order, or accept a UUID prefix.
2. Obtain the absolute service `DFX_LOG_DIR`. Prefer an explicit directory from
   the user or task context; otherwise use the process `DFX_LOG_DIR`.
3. Run exactly one render command in the environment containing the matching
   Problem Locator package:

```text
python -m problem_locator render-journey --case-id <case-id> --log-dir <absolute-log-dir>
```

   Omit `--log-dir` only when `DFX_LOG_DIR` is already present in the process
   environment.
4. Continue only when the command exits 0 and stdout contains a valid receipt.
   Read output paths from that receipt; do not construct or scan for them.
5. For a summary, status, conclusion, or next-step request, read `brief_log`.
   For a complete chain, stage-by-stage explanation, failure cause, or raw record
   references, read `detailed_log`; read `brief_log` first when an overview helps.
6. Link both generated files in the final answer. Base every claim on their text.

## Interpretation Rules

- Treat `terminal=false` as a current snapshot. State clearly that it is not a
  final root-cause conclusion.
- For `FAILED` or `CANCELLED`, read the detailed log before explaining the stop.
- Preserve event names, enums, IDs, error codes, and file references exactly.
- Use the `brief.log` critical-path ranking as the primary answer to performance
  questions, and use the per-Job evidence in `detailed.log` to explain the
  ranking. Describe it as a major time source, not proof of an abnormality.
- Preserve `COMPLETE`, `PARTIAL`, or `UNAVAILABLE` Agent telemetry coverage and
  its reason code. When stream detail is unavailable, still report the base
  Backend/queue/wait timing; do not infer model timing from debug or execution
  logs.
- Treat model API duration as the CLI-reported aggregate. Treat thinking, text,
  and tool windows as server observations that can overlap and must not be added
  to the Case critical-path totals.
- Do not infer events absent from the rendered files.

## Failures

- Exit 2: report the invalid or missing Case ID and request the exact ID. Do not
  select another Case.
- Exit 3: report the configuration or `journey.jsonl:<line>` validation error.
  If the old key is present, instruct the user to remove `DFX_LOG_FILE` and use
  `DFX_LOG_DIR`. Do not read stale generated files.
- Exit 4: report that publishing the derived logs failed and leave the source
  untouched. Do not read either derived file because the pair may be inconsistent.
