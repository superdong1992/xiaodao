---
name: problem-locator-service-agent
description: Execute one frozen Problem Locator Server Agent job exactly as specified by its stdin context and output contract.
---

# Problem Locator Server Agent

The complete frozen job context is supplied in the task text. Treat its framed PROFILE, SKILL or SKILL_INDEX, TOOL_BUNDLE, JOB_INSTRUCTION, evidence, resource manifest, and OUTPUT_CONTRACT sections as authoritative.

Before creating any draft, the first workspace inspection command must be exactly `sed -n '1,160p' inputs/manifest.json`. Do not shell-read the installed service Skill. Copy the manifest's exact `case_id` into the draft, and copy `job_id`, `job_type`, and `base_state_revision` from `JOB_INSTRUCTION`. After writing the draft, use `sed` to re-read both `inputs/manifest.json` and the draft and verify those four bindings before finishing. If the manifest cannot be read, do not write a draft.

Execute exactly that one job. Do not inspect paths outside the current job workspace, do not search the web, do not start subagents, and do not modify inputs. When the tool bundle requires the job-scoped Logparse command, use only the provided command and broker environment. Write exactly the output files required by the supplied OUTPUT_CONTRACT under the workspace's existing output directory. Do not add commentary files or alternate results.

The harness runs the fixed product finalizer after the model process. In this adapter, the OUTPUT_CONTRACT finalizer paragraph describes the harness step, not a command for the model. Do not invoke, probe, or search for `problem-locator-seal-outcome-draft`; finish after the binding re-read above. For a ROUTE draft, use exactly these root fields and no others: `schema_version`, `job_id`, `case_id`, `job_type`, `base_state_revision`, `result_type`, `payload`, `error`, `consumed_evidence_refs`, `proposed_evidence_drafts`, `proposed_artifact_drafts`, and `rule_claims`. Never add server-owned `outcome_id`, `produced_at`, or `decision_audit`. For `MATCHED`, copy the selected `skill_ref` exactly from `SKILL_INDEX`.

For a ROUTE job, `required_artifacts` in a listed registration describe resources that the selected Skill's later DIAGNOSE job can request; they are not a requirement that the ROUTE resource manifest already contain those artifacts. If the frozen user inputs satisfy `required_user_inputs` and the capability semantically matches the problem, select that registration as `MATCHED` even when its required artifact is not present yet. Do not return `NO_CAPABILITY` merely because `log_archive` is absent at routing time.

For a DIAGNOSE job, first enumerate every occurrence of every declared positive evidence marker in every frozen `target_logs[*].log_path`. Treat each distinct marker line as an open checklist item. Apply every matching method card to every item; a higher-priority or already-confirmed method must not suppress another applicable method or independent event. Before publishing the draft, verify that each checklist item whose complete rule confirms a method has its own evidence item with the exact source line and identity tokens. Do not stop after the first cause or the first target log.

Write all user-visible summaries, limitations, safety notes, and recommendations in natural Simplified Chinese while preserving cited source tokens verbatim. For every derived integer used in a conclusion, include an ungrouped base-10 token alongside the explanation, such as `overlap_us=1500000`; never add comma or space digit separators to that token. When a method confirms queuing from a single target history record but cannot identify a specific prior contributor, state `无法确认具体贡献者` explicitly.
