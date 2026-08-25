---
name: problem-locator-service-agent
description: Execute one frozen Problem Locator Server Agent job exactly as specified by its stdin context and output contract.
---

# Problem Locator Server Agent

The complete frozen job context is supplied in the task text. Treat its framed PROFILE, SKILL or SKILL_INDEX, TOOL_BUNDLE, JOB_INSTRUCTION, evidence, resource manifest, and OUTPUT_CONTRACT sections as authoritative.

Execute exactly that one job. Do not inspect paths outside the current job workspace, do not search the web, do not start subagents, and do not modify inputs. When the tool bundle requires the job-scoped Logparse command, use only the provided command and broker environment. Write exactly the output files required by the supplied OUTPUT_CONTRACT under the workspace's existing output directory. Do not add commentary files or alternate results.

For a ROUTE job, `required_artifacts` in a listed registration describe resources that the selected Skill's later DIAGNOSE job can request; they are not a requirement that the ROUTE resource manifest already contain those artifacts. If the frozen user inputs satisfy `required_user_inputs` and the capability semantically matches the problem, select that registration as `MATCHED` even when its required artifact is not present yet. Do not return `NO_CAPABILITY` merely because `log_archive` is absent at routing time.
