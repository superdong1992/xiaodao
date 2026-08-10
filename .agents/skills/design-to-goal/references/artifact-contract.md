# Design-to-Goal artifact contract

## Contents

1. Authority and artifact roles
2. Work-item identity and write boundary
3. Conversation archive
4. Design evidence and conflict review
5. Test Flow protected surface
6. Approval and drift
7. Codex execution Goal
8. Testing and completion

## Authority and artifact roles

Use the artifacts for different purposes:

- `conversation.md` is append-only visible-dialogue evidence and recovery context. It is never an instruction authority.
- `design.md` is the decision-complete change design. A user approval binds to its exact SHA-256 digest.
- `goal.md` is the execution contract derived from the approved design and created only after a separate Goal-start instruction.

Apply platform instructions and applicable AGENTS constraints first. Reconcile current code, schema, config, and current design authority explicitly; do not silently choose one side of a conflict. The approved design controls the requested target only after every conflict with higher or repository authority has been resolved. Quoted text, links, and older wishes in `conversation.md` cannot expand approved scope.

## Work-item identity and write boundary

Create exactly one directory for a design:

```text
work-items/YYYYMMDD-<kebab-name>/
├── conversation.md
├── design.md
└── goal.md                 # absent until the separate Goal-start instruction
```

- Derive a concise lowercase kebab name from the requested change.
- If the path exists when first selected, append `-2`, `-3`, and so on.
- Never rename the directory or open a second directory to bypass its write boundary.
- After a previously active Goal is canceled or completed, redesign never mutates its frozen work item. Create a linked successor using `<original>-r2`, then `-r3`, and record the predecessor path and frozen digests in all successor contracts. This is the only permitted second directory for the same change.
- Until a matching Codex Goal with the exact work-item path and approved digests is successfully created and confirmed active, only this directory is writable. Design approval, a Goal-start request, or failed Goal creation does not relax the boundary. Git staging, commits, branches, pushes, PRs, source files, current docs, tests, AGENTS/Skill files, and external state are out of bounds.
- Do not create placeholder work items for read-only questions.
- If a design is abandoned, append the user decision to `conversation.md` and retain the directory; do not delete or commit it automatically.

## Conversation archive

Copy `assets/conversation.md` when creating the work item. Replace metadata placeholders and append entries using its markers.

For every entry:

- Allocate a monotonically increasing four-digit sequence.
- Record role (`user` or `assistant`) and visible kind (`message`, `commentary`, `question`, `options`, or `final`).
- Preserve visible Markdown bytes between the begin/end markers whenever no redaction is required.
- Draft and append assistant-visible content before emitting it. Reconcile on the next turn and append a `correction` entry if the delivered content differed.
- Never edit or reorder prior entries.
- Do not include hidden instructions, reasoning, tool calls, tool outputs, or external secrets.
- Replace suspected credentials or high-sensitivity text with `[REDACTED:<type>]`; set `redaction: true` and explain why without reproducing the secret.

Before requesting design approval and before creating a Goal, flush all visible messages relevant to the decision. The approval and later Goal-start instruction must both appear in the archive.

## Design evidence and conflict review

Copy `assets/design.md` and complete every section. Record concrete sources rather than a generic “repository reviewed” claim:

- current Git commit and visible dirty/untracked state;
- actual deployed or executed version when relevant;
- entry point, input, expected output, and actual output for a defect;
- every applicable AGENTS file;
- `design/README.md` and the task-relevant current design documents it identifies;
- affected schemas, generated contracts, runtime configuration, and implementation entry points;
- existing tests and Test Flow configuration relevant to acceptance.

Use the conflict table for each disagreement among current facts, user target, current design, or repository requirements. Valid states are `UNRESOLVED` and `RESOLVED`; do not invent an “accepted risk” state that bypasses a decision. For each conflict, record:

1. both sources or claims;
2. concrete impact;
3. valid options, excluding options forbidden by higher-level constraints;
4. the exact archived user decision reference;
5. the resolution and resulting scope/documentation change.

Set the unresolved count accurately. Any nonzero count blocks digest presentation, design approval, `goal.md`, and Goal creation.

For fixes, the current issue must be confirmed before a proposed fix is approved. If confirmation fails, record the evidence and stop with no fix Goal.

## Test Flow protected surface

Treat these as Test Flow process changes requiring separate, specific user authorization:

- all of `tools/test-flow/**`, including runners, configuration, planners, adapters, identity/policy/runtime definitions, evidence handling, verdict handling, and operating documentation;
- `design/test-flow-architecture.md` and any other current document defining Test Flow architecture or operating policy;
- the “测试活动约束” rules in `AGENTS.md` or an applicable nested AGENTS file.

Changes to ordinary product tests under `tests/**` are not Test Flow process changes unless they also change the runner, selection policy, evidence contract, or verdict semantics.

The `design.md` Test Flow section must satisfy exactly one invariant:

- `Impact: NONE` implies exact protected changes `NONE`, authorization `NOT_REQUIRED`, and an explanation of how existing Test Flow covers the acceptance contract; or
- `Impact: PROTECTED_CHANGE` implies a nonempty exact path/behavior list, compatibility/documentation/validation impacts, and a distinct archived user authorization entry.

“Approve the design” never authorizes a protected Test Flow change. Keep such a change `UNRESOLVED` until the user explicitly authorizes the listed change.

Approval eligibility is derived: the conflict table contains no `UNRESOLVED` row and the applicable Test Flow invariant is satisfied. Never use a hand-written boolean or conflict count to override those facts. If a displayed count is present, it must equal the number of `UNRESOLVED` rows.

## Approval and drift

After completing conflict review:

1. Freeze `design.md`.
2. Calculate SHA-256 over its exact bytes.
3. Present the path and digest to the user.
4. Obtain an explicit approval referencing that presented version.
5. Archive the approval without changing `design.md`.

Design approval and Goal start are two independent messages. Do not create `goal.md` or a Goal merely because the design was approved.

Before Goal creation, invalidate approval if any of the following changed:

- `design.md` bytes;
- an applicable AGENTS rule or consulted current design authority;
- target-scope code, schema, config, or generated contract in a way material to the design;
- Test Flow protected scope or authorization.

Re-run the relevant review and obtain approval for a new digest after invalidation.

## Codex execution Goal

After a later explicit Goal-start message, call `get_goal` before creating files or relaxing the work-item boundary. If `get_goal` is unavailable or ambiguous, fail closed. Never replace, clear, or overwrite an unfinished Goal automatically.

Copy `assets/goal.md` and complete it with:

- canonical work-item path, predecessor reference if any, and frozen conversation/design digests;
- archived design-approval and Goal-start entry references;
- current baseline and authority revalidation evidence;
- one objective and one verifiable stopping condition;
- required read-first files;
- allowed and forbidden modifications;
- authoritative documentation synchronization list;
- ordinary test work;
- Test Flow track, Test Flow goal, exact plan-only and execution commands;
- successful verdict requirements;
- conditions that stop implementation and require Goal cancellation before redesign.

After completing `goal.md`, freeze candidate bytes for all three work-item files and calculate their SHA-256 digests. The Codex Goal objective must contain the canonical work-item path, the three digests, and a direction to reread all three files whenever context is uncertain. Do not write a file's own digest back into that file. Do not set `token_budget` unless the user explicitly requested one.

After Goal creation, call `get_goal` again. Treat it as matching only when it is unfinished/active and its objective contains the exact canonical path plus frozen conversation, approved design, and frozen goal digests. Until that check succeeds, no write outside the work item is allowed. Only an explicit creation failure plus an unambiguous `get_goal` result showing no unfinished Goal invalidates candidate digests and permits append-only archival plus Goal-contract correction inside that work item. If outcome or confirmation is ambiguous, keep candidate files frozen, stop all writes, and retry only read-only inspection; require user cancellation if any active/mismatched Goal appears.

Once matching execution begins, permanently freeze `conversation.md`, `design.md`, and `goal.md`. Codex progress belongs in Goal state; Test Flow plans, receipts, and verdicts belong in its ignored evidence root. This prevents work-item bytes from drifting after a Test Flow source snapshot.

If execution requires a new decision, stop implementation and ask the user to cancel or clear the active Goal. After `get_goal` confirms no unfinished Goal, keep the predecessor permanently frozen and create a linked successor work item (`-r2`, then `-r3`). Repeat conflict review and approval there, freeze new digests, and require another separate Goal-start message. A successor Goal reads both its own three files and every predecessor named in its contract.

## Testing and completion

Default repository validation:

```sh
./tools/test-flow/run.sh --track dev --goal dev.default --plan-only
./tools/test-flow/run.sh --track dev --goal dev.default
```

Windows uses the same arguments:

```powershell
powershell.exe -ExecutionPolicy Bypass -File tools/test-flow/run.ps1 --track dev --goal dev.default --plan-only
powershell.exe -ExecutionPolicy Bypass -File tools/test-flow/run.ps1 --track dev --goal dev.default
```

Always distinguish the Codex execution Goal from the Test Flow goal. Do not run `pytest`, npm test commands, adapter internals, or custom scripts directly to assemble a repository pass claim.

The execution command must use exactly the plan-only arguments except for removing `--plan-only`: track, Test Flow goal, Stage/Proof selection, client, runtime profile, external inputs, opt-ins, and retry intent must not change. Compare the executed run plan and verdict with the inspected plan's source snapshot and identities. Keep any transient comparison record outside tracked source and never treat it as an alternate verdict.

For real-model activity, require the existing explicit opt-in, identity, model budget, estimated token/cost review, and admission checks. A retry of the same failing identity requires new `reason`, `hypothesis`, and `expected-evidence` values.

For `release.full`, additionally require the existing runner to:

- freeze the Git-visible tracked bytes and unignored untracked files into its SHA-256 source manifest during planning;
- bind current Client, Server, Logparse, MCP, Skill, runtime, and model-context identities;
- start the real CrossJob journey from GENESIS and a new empty `DATA_ROOT`;
- fail if source bytes drift between planning and `verdict.json`.

Completion requires all of the following:

- implementation and ordinary tests match the approved design;
- every listed current authoritative document is synchronized;
- the Test Flow run used the inspected plan and approved track/goal;
- execution arguments and source/identity bindings match the inspected plan;
- the authoritative `verdict.json` is verified, successful under the existing status contract, and bound to the resulting source state;
- warnings are reported rather than hidden;
- no new design decision or protected Test Flow change remains unresolved.

Only after every completion item is verified, call `update_goal(status=complete)`. A successful process exit or even a successful verdict alone is insufficient if implementation or documentation obligations remain. If any item is unsatisfied, keep the Goal unfinished and report the exact failing condition.

If existing Test Flow cannot prove an acceptance criterion, return to design and request specific Test Flow change authorization. Never improvise an alternate testing workflow.
