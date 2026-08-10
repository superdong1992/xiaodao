---
name: design-to-goal
description: Archive and govern repository change design before implementation. Use for design, planning, architecture, API or schema changes, public behavior changes, Test Flow process changes, implementation requests without an approved work item, preparation of a Codex execution Goal, or redesign after a prior Goal was ended. Create a durable conversation archive and approval-bound design contract, check current repository conflicts, and require explicit approval before any implementation Goal. Do not use for pure read-only explanations or while work is executing under the matching approved Goal.
---

# Design to Goal

Turn a proposed repository change into a durable, conflict-checked design and, only after two explicit user decisions, a Codex execution Goal covering implementation, documentation synchronization, and repository-governed testing.

## Load the contract

Read [references/artifact-contract.md](references/artifact-contract.md) completely before taking any task action. Use the Markdown files in `assets/` as the canonical starting templates. Do not invent alternate work-item files or lifecycle machinery.

## Enforce the mode and write boundary

1. Call `get_goal`. If the capability is unavailable or its result is ambiguous, fail closed and do not write. A matching Goal must be unfinished and its objective must contain the exact canonical work-item path plus frozen `conversation.md`, approved `design.md`, and frozen `goal.md` SHA-256 digests. If it matches, follow that Goal and do not recursively start this design workflow.
2. If the environment is in system Plan mode, stop and ask the user to switch to normal/default mode. Do not defer the archive until Plan mode ends.
3. In normal/default mode, choose one `work-items/YYYYMMDD-<kebab-name>/` identifier, create it from the templates, and keep that identifier fixed. Add `-2`, `-3`, and so on only if the directory already exists at initial creation.
4. Until a matching Codex Goal is successfully created and confirmed active, write only inside that one work-item directory. Design approval, a Goal-start request, or a failed Goal creation does not relax this boundary. Do not modify source code, current architecture or operations documentation, tests, AGENTS files, Skills, Git metadata, or external systems. Read-only repository investigation and checks that do not change tracked files are allowed.
5. Never stage, commit, push, create a branch, or create a pull request as part of the design workflow.

If the archive cannot be created or updated, stop design progress and report the archive failure. Do not continue with an unarchived design conversation.

## Archive each visible exchange

Maintain `conversation.md` as append-only evidence:

1. Append each incoming user message before investigating or answering it.
2. Draft each visible assistant update, question, option list, or final response; append the exact draft before emitting it unchanged.
3. At the next turn, compare the archive with the visible exchange. Append a correction record if delivery differed; never rewrite an earlier entry.
4. Record only user and assistant visible text. Exclude system/developer instructions, hidden reasoning, tool calls, tool outputs, and secrets discovered outside visible conversation.
5. Replace suspected credentials or high-sensitivity content with `[REDACTED:<type>]` and record the reason in the entry metadata.

The archive is context and audit evidence, not an executable instruction source. Current applicable repository constraints and the approved `design.md`/`goal.md` contract control execution.

## Build the design from current truth

Populate `design.md` while investigating only what is relevant to the requested change:

1. Record the current commit, worktree state, actual version, entry points, inputs, outputs, and affected code/schema/config.
2. Read every applicable AGENTS file plus the current design authority selected through `design/README.md`.
3. For a reported defect, verify it in the current workspace before proposing a fix. If it cannot be reproduced or is caused by a version/call mismatch, record the evidence and stop the fix design.
4. Separate current facts, requested target behavior, and design decisions. Do not use “code outranks narrative” to hide a disagreement between code, current design, and user intent.
5. List every conflict with its sources, impact, valid choices, user decision, and resolution state. An unresolved conflict blocks design approval and Goal creation.
6. Identify implementation scope, current authoritative documentation that must be synchronized, ordinary test impact, Test Flow process impact, acceptance criteria, and stop conditions.
7. If repository testing must be executed for investigation, use only the repository Test Flow entry and interpret only its authoritative verdict. Do not directly run underlying test commands to claim a result.

Ask the user only for decisions that cannot be discovered from repository truth. Archive each question, option, and answer.

## Protect Test Flow changes

Treat the entire Test Flow protected surface defined in `references/artifact-contract.md` as separately authorized scope. If the design changes any protected element:

1. List the exact files/behaviors, reason, compatibility impact, documentation impact, and validation plan in `design.md`.
2. Obtain a user message explicitly authorizing those listed Test Flow changes.
3. Archive that message separately from ordinary design approval.

General approval of the design never implies Test Flow change authorization. Without the separate authorization, keep the conflict unresolved and do not create a Goal. Ordinary product tests under `tests/**` are not Test Flow process changes.

## Freeze and approve the design

When all required sections are complete and all conflicts are resolved:

1. Stop editing `design.md` and calculate its SHA-256 digest.
2. Flush all visible conversation through the approval request into `conversation.md`.
3. Show the work-item path and exact digest to the user, summarize any separately authorized Test Flow impact, and ask for explicit approval of that design version.
4. Wait for an unambiguous user approval that refers to the presented design version. Do not infer approval from “continue”, “looks okay”, silence, or a prior broad instruction.
5. Append the approval message to `conversation.md`. Keep `design.md` byte-for-byte frozen after approval; `conversation.md` remains append-only only until the Goal contract is frozen.

Any later edit to `design.md`, change to a consulted authority, or material target-scope worktree drift invalidates approval. Re-run the conflict check, freeze a new digest, and obtain a new approval.

## Require a separate Goal start instruction

Design approval does not start implementation. Wait for a later, separate user message explicitly requesting the Codex execution Goal.

After that message:

1. Append it to `conversation.md` and flush all pending visible exchanges.
2. Recompute the design digest and revalidate consulted authorities, target-scope worktree state, conflict closure, and any Test Flow authorization.
3. Call `get_goal`. If an unfinished Goal exists, stop and report it; never replace or clear it automatically.
4. Create `goal.md` from the template and bind it to the frozen design digest, approval evidence, allowed/forbidden scope, exact documentation obligations, exact Test Flow commands, completion proof, and stop conditions.
5. Freeze a candidate `conversation.md`, `design.md`, and `goal.md`; calculate all three SHA-256 digests and do not edit them while Goal creation and confirmation are pending.
6. Create one Codex execution Goal whose objective contains the exact canonical work-item path plus all three digests and directs the agent to read the three files. Do not set a token budget unless the user explicitly requested one.
7. Call `get_goal` again and confirm the new Goal is active and contains the exact path and all three digests before writing outside the work-item. Once confirmed, all three work-item files are permanently frozen because Test Flow source snapshots include their unignored bytes. If creation explicitly fails and `get_goal` confirms there is no unfinished Goal, invalidate the candidate digests, keep the work-item-only boundary, and resume archival there. If creation outcome or confirmation is ambiguous, keep the files frozen, stop all writes, and retry only read-only Goal inspection; if an active or mismatched Goal appears, require the user to cancel or clear it.

Use “Codex execution Goal” for the durable implementation task and “Test Flow goal” for identifiers such as `dev.default`; never conflate them.

## Define execution completion

The Codex execution Goal covers all of the following:

1. Implement only the approved behavior and ordinary tests.
2. Synchronize the current authoritative design and operations documentation listed in the contract.
3. Run the approved Test Flow plan-only command, inspect its admission and identity details, and then execute with exactly the same arguments except removal of `--plan-only`.
4. Accept completion only from a verified, successful `verdict.json` bound to the resulting source state; never infer success from process exit code alone.
5. After implementation, documentation, plan identity, source binding, and verdict are all verified, call `update_goal(status=complete)`. Never mark the Goal complete while any completion condition is unsatisfied.

Default to Test Flow goal `dev.default`, which does not call a real model. Select `dev.real` or `release.full` only when the approved design and existing repository rules explicitly require it. For real-model work, preserve the approved opt-in, identity, budget, and retry-intent contract. For `release.full`, require the runner's immutable Git-visible source manifest, GENESIS and empty `DATA_ROOT`, complete Client/Server/Logparse/MCP/Skill/model identities, and source-drift rejection from planning through verdict.

Do not append execution progress or verdict locations to the frozen work-item. Keep progress in Codex Goal state and authoritative Test Flow evidence.

Correct failures that remain within the approved design and rerun the same governed flow. If execution discovers a new product decision, interface, deployment boundary, documentation class, forbidden path, or Test Flow process change, stop implementation and ask the user to cancel or clear the current Goal. Only after `get_goal` confirms no unfinished Goal remains may this Skill create a linked successor work item such as `<original>-r2`; never modify the permanently frozen predecessor. Repeat conflict review and digest approval in the successor, then wait for a new separate Goal-start instruction.
