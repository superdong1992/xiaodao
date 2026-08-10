# Codex Execution Goal Contract

## Binding

- Work item: `{{WORK_ITEM_ID}}`
- Canonical work-item path: `{{CANONICAL_WORK_ITEM_PATH}}`
- Predecessor work item chain: `NONE|{{PREDECESSOR_CANONICAL_PATHS_AND_DIGESTS}}`
- Frozen conversation SHA-256: `{{CONVERSATION_SHA256}}`
- Approved design: `{{DESIGN_PATH}}`
- Approved design SHA-256: `{{DESIGN_SHA256}}`
- Design approval conversation entry: `{{APPROVAL_ENTRY}}`
- Separate Goal-start conversation entry: `{{GOAL_START_ENTRY}}`
- Authority and target-scope revalidation: `{{REVALIDATION_EVIDENCE}}`
- Frozen goal SHA-256: Compute after finalizing this file and bind it in the Codex Goal objective; do not write the resulting digest back here.

## Objective and stopping condition

- Objective: `{{OBJECTIVE}}`
- Stop successfully only when: `{{VERIFIABLE_STOPPING_CONDITION}}`

## Read first and reread on uncertainty

1. `{{WORK_ITEM_PATH}}/conversation.md` for visible context evidence only.
2. `{{WORK_ITEM_PATH}}/design.md` for the approved behavior and scope.
3. `{{WORK_ITEM_PATH}}/goal.md` for this execution contract.
4. {{PREDECESSOR_WORK_ITEM_FILES_OR_NONE}}
5. {{REPOSITORY_AUTHORITIES}}

## Allowed changes

{{ALLOWED_CHANGES}}

## Forbidden changes

{{FORBIDDEN_CHANGES}}

## Implementation and ordinary tests

{{IMPLEMENTATION_AND_TEST_OBLIGATIONS}}

## Authoritative documentation synchronization

Copy the approved design list exactly; do not broaden it during execution.

| Exact path | Required synchronization | Completion verification |
| --- | --- | --- |
| `{{DOCUMENT_PATH_OR_NONE}}` | {{REQUIRED_CHANGE_OR_NONE}} | {{VERIFICATION_OR_NONE}} |

## Test Flow execution

- Track: `{{TEST_FLOW_TRACK}}`
- Test Flow goal: `{{TEST_FLOW_GOAL}}`
- Plan-only command: `{{PLAN_ONLY_COMMAND}}`
- Execution command: `{{EXECUTION_COMMAND}}`
- Argument invariant: Execution arguments are byte-for-byte the plan-only arguments except that `--plan-only` is removed.
- Plan comparison: Confirm source snapshot, track/goal, Stage/Proof selection, client, runtime profile, external inputs, admission, identities, opt-ins, budgets, and retry intent match.
- Required verdict: A verified, successful `verdict.json` bound to the resulting source state; an exit code alone is not proof.
- Separately authorized Test Flow changes: `NONE|{{AUTHORIZATION_ENTRY_AND_SCOPE}}`
- Release-only closure: `NOT_APPLICABLE|immutable Git-visible SHA-256 manifest; full Client/Server/Logparse/MCP/Skill/model identities; GENESIS; empty DATA_ROOT; source-drift rejection`

## Stop and require Goal replacement conditions

Stop without expanding scope and ask the user to cancel or clear the current Goal when implementation discovers:

- a new product or interface decision;
- a new deployment or compatibility boundary;
- an authoritative documentation class not covered by the approved design;
- a forbidden path or material authority drift;
- a Test Flow process change not separately authorized;
- an acceptance criterion that existing Test Flow cannot prove.

## Immutability after Goal creation

Freeze `conversation.md`, `design.md`, and this file before creating the Codex Goal. Bind their digests in the Goal objective. Do not modify any work-item file during execution or after Test Flow planning; keep progress in Codex Goal state and results in authoritative Test Flow evidence.

After every implementation, documentation, plan-identity, source-binding, and verdict condition is verified, call `update_goal(status=complete)`. Never complete the Goal from exit code or verdict alone.
